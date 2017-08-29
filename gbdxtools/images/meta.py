from __future__ import print_function
import abc
import types
import os
import random
from functools import wraps, partial
from collections import Container
from six import add_metaclass

from shapely import ops
from shapely.geometry import box, shape, mapping
from shapely import wkt

import skimage.transform as tf

import pyproj
import dask
import dask.array as da
import numpy as np

from affine import Affine

from gbdxtools.ipe.io import to_geotiff
from gbdxtools.ipe.util import RatPolyTransform, pad_safe_positive, pad_safe_negative, IPE_TO_DTYPE

try:
    from matplotlib import pyplot as plt
    has_pyplot = True
except:
    has_pyplot = False

try:
    xrange
except NameError:
    xrange = range

num_workers = int(os.environ.get("GBDX_THREADS", 8))
threaded_get = partial(dask.threaded.get, num_workers=num_workers)


@add_metaclass(abc.ABCMeta)
class DaskMeta(object):
    """
    A DaskMeta is an interface for the required attributes for initializing a dask Array
    """
    @abc.abstractproperty
    def dask(self):
        pass

    @abc.abstractproperty
    def name(self):
        pass

    @abc.abstractproperty
    def chunks(self):
        pass

    @abc.abstractproperty
    def dtype(self):
        pass

    @abc.abstractproperty
    def shape(self):
        pass

    def infect(self, target):
        assert isinstance(target, da.Array), "DaskMeta can only be attached to Dask Arrays"
        assert len(target.shape) in [2, 3], "target must be a dask array with 2 or 3 dimensions"
        target.__dict__["__daskmeta__"] = property(lambda s: self, DaskImage.__set_daskmeta__)
        return target


@add_metaclass(abc.ABCMeta)
class DaskImage(da.Array):
    """
    A DaskImage is a 2 or 3 dimension dask array that contains implements the `__daskmeta__` interface.
    """

    @property
    def __daskmeta__(self):
        """ Should return a DaskMeta """
        pass

    @__daskmeta__.setter
    def __set_daskmeta__(self, obj):
        self.__dict__["__daskmeta__"] = property(lambda s: obj, self.__set_daskmeta__)

    @classmethod
    def __subclasshook__(cls, C):
        if cls is DaskImage:
            try:
                if(issubclass(C, da.Array) and
                   any("__daskmeta__" in B.__dict__ for B in C.__mro__)):
                    return True
            except AttributeError:
                pass
        return NotImplemented

    def __getattribute__(self, name):
        fn = object.__getattribute__(self, name)
        if(isinstance(fn, types.MethodType) and
           any(name in C.__dict__ for C in self.__class__.__mro__)):
            @wraps(fn)
            def wrapped(*args, **kwargs):
                result = fn(*args, **kwargs)
                if isinstance(result, da.Array) and len(result.shape) in [2,3]:
                    result = super(DaskImage, self.__class__).__new__(self.__class__,
                                                                      result.dask, result.name, result.chunks,
                                                                      result.dtype, result.shape)
                    result.__dict__.update(self.__dict__)
                return result
            return wrapped
        else:
            return fn

    @classmethod
    def create(cls, dm):
        """
        Given a dask meta object, construct a dask array, attach dask meta object.
        """
        assert isinstance(dm, DaskMeta), "argument must be an instance of a DaskMeta subclass"
        with dask.set_options(array_plugins=[dm.infect]):
            obj = da.Array.__new__(cls, dm.dask, dm.name, dm.chunks, dm.dtype, dm.shape)
            return obj

    def read(self, bands=None):
        """ Reads data from a dask array and returns the computed ndarray matching the given bands """
        arr = self
        if bands is not None:
            arr = self[bands, ...]
        return arr.compute(get=threaded_get)

    def randwindow(self, window_shape):
        row = random.randrange(window_shape[0], self.shape[1])
        col = random.randrange(window_shape[1], self.shape[2])
        return self[:, row-window_shape[0]:row, col-window_shape[0]:col]

    def iterwindows(self, count=64, window_shape=(256, 256)):
        if count is None:
            while True:
                yield self.randwindow(window_shape)
        else:
            for i in xrange(count):
                yield self.randwindow(window_shape)


@add_metaclass(abc.ABCMeta)
class GeoImage(Container):
    _default_proj = "EPSG:4326"

    # def __geo_interface__(self):
    #     pass

    # def __geo_transform__(self):
    #     pass

    @classmethod
    def __subclasshook__(cls, C):
        # Must be a numpy-like, with __geo_transform__, and __geo_interface__
        if(issubclass(C, DaskImage) or issubclass(C, np.ndarray) and
           any("__geo_transform__" in B.__dict__ for B in C.__mro__) and
           any("__geo_interface__" in B.__dict__ for B in C.__mro__)):
            return True
        raise NotImplemented

    @property
    def affine(self):
        """ The affine transformation of the image """
        # TODO add check for Ratpoly or whatevs
        return self.__geo_transform__._affine

    @property
    def bounds(self):
        """ The spatial bounding box for the image """
        return shape(self).bounds

    @property
    def proj(self):
        """ The projection of the image """
        return self.__geo_transform__.proj

    def aoi(self, **kwargs):
        """ Subsets the Image by the given bounds

        kwargs:
            bbox: optional. A bounding box array [minx, miny, maxx, maxy]
            wkt: optional. A WKT geometry string
            geojson: optional. A GeoJSON geometry dictionary

        Returns:
            image (ndarray): an image instance
        """
        g = self._parse_geoms(**kwargs)
        if g is None:
            return self
        else:
            return self[g]

    def geotiff(self, **kwargs):
        """ Creates a geotiff on the filesystem

        kwargs:
            path (str): optional. The path to save the geotiff to.
            bands (list): optional. A list of band indices to save to the output geotiff ([4,2,1])
            dtype (str): optional. The data type to assign the geotiff to ("float32", "uint16", etc)
            proj (str): optional. An EPSG proj string to project the image data into ("EPSG:32612")

        Returns:
            path (str): the path to created geotiff
        """
        if 'proj' not in kwargs:
            kwargs['proj'] = self.proj
        return to_geotiff(self, **kwargs)

    def warp(self, dem=0, rpcs=None, proj=None, **kwargs):
        """
          Delayed warp across an entire AOI or Image
          creates a new dask image by deferring calls to the warp_geometry on chunks
        """
        img_md = self.ipe.metadata["image"]
        im_full = self.__class__(img_md['imageId'], product='1b')
        x_size = img_md["tileXSize"]
        y_size = img_md["tileYSize"]

        # Create an affine transform to convert between real-world and pixels
        gsd = kwargs.get("gsd", im_full.ipe.metadata["rpcs"]["gsd"])
        gtf = Affine.from_gdal(im_full.bounds[0], gsd, 0.0, im_full.bounds[3], 0.0, -1 * gsd)

        ll = ~gtf * (self.bounds[:2])
        ur = ~gtf * (self.bounds[2:])
        x_chunks = int((ur[0] - ll[0]) / x_size) + 1
        y_chunks = int((ll[1] - ur[1]) / y_size) + 1

        daskmeta = {
            "dask": {},
            "chunks": (img_md["numBands"], y_size, x_size),
            "dtype": IPE_TO_DTYPE[img_md["dataType"]],
            "name": "warp-{}".format(self.ipe_id),
            "shape": (img_md["numBands"], y_chunks * y_size, x_chunks * x_size)
        }

        def px_to_geom(xmin, ymin):
            xmax = int(xmin + img_md["tileXSize"])
            ymax = int(ymin + img_md["tileYSize"])
            bounds = list((gtf * (xmin, ymax)) + (gtf * (xmax, ymin)))
            return box(*bounds)

        for y in xrange(y_chunks):
            for x in xrange(x_chunks):
                xmin = ll[0] + (x * x_size)
                ymin = ur[1] + (y * y_size)
                geometry = px_to_geom(xmin, ymin)
                daskmeta["dask"][(daskmeta["name"], 0, y - img_md['minTileY'], x - img_md['minTileX'])] = (im_full.warp_geometry, geometry, dem, rpcs, proj, gsd)

        return GeoDaskWrapper(daskmeta, self)


    def warp_geometry(self, geometry, dem=0, rpcs=None, proj=None, gsd=None, gtf=None, **kwargs):
        """
          Warps a geometry
          pads the image aoi and creates a pixel translation matrix to warp data to
        """
        if proj:
            xmin, ymin, xmax, ymax = self._reproject(geometry, from_proj=self.proj, to_proj=proj).bounds
        else:
            xmin, ymin, xmax, ymax = geometry.bounds

        if gtf is None:
            if rpcs is not None:
                gtf = RatPolyTransform.from_rpcs(rpcs)
            else:
                gtf = self.__geo_transform__

        if gsd is None:
            if hasattr(gtf, "gsd") and gtf.gsd is not None:
                gsd = gtf.gsd
            else:
                gsd = self.ipe.metadata["rpcs"]["gsd"]

        x = np.linspace(xmin, xmax, num=int((xmax-xmin)/gsd))
        y = np.linspace(ymax, ymin, num=int((ymax-ymin)/gsd))
        xv, yv = np.meshgrid(x, y, indexing='xy')

        if isinstance(dem, np.ndarray):
            # TODO what do we do about projection here?
            dem = tf.resize(np.squeeze(dem), xv.shape, preserve_range=True)

        transpix = gtf.rev(xv, yv, z=dem, _type=np.float32)[::-1]

        xpad, ypad = kwargs.get("padsize", (2,2))
        psn = partial(pad_safe_negative, transpix=transpix, ref_im=self)
        psp = partial(pad_safe_positive, transpix=transpix, ref_im=self)
        ymint, xmint = (psn(padsize=ypad, ind=0), psn(padsize=xpad, ind=1))
        ymaxt, xmaxt = (psp(padsize=ypad, ind=0), psp(padsize=xpad, ind=1))
        shifted = np.stack([transpix[0,:,:] - ymint, transpix[1,:,:] - xmint])

        data = self[:,ymint:ymaxt,xmint:xmaxt].read(quiet=True)
        return np.rollaxis(np.dstack([tf.warp(data[b,:,:].squeeze(), shifted, preserve_range=True) for b in xrange(data.shape[0])]), 2, 0)

    def _parse_geoms(self, **kwargs):
        """ Finds supported geometry types, parses them and returns the bbox """
        bbox = kwargs.get('bbox', None)
        wkt_geom = kwargs.get('wkt', None)
        geojson = kwargs.get('geojson', None)
        if bbox is not None:
            g = box(*bbox)
        elif wkt_geom is not None:
            g = wkt.loads(wkt_geom)
        elif geojson is not None:
            g = shape(geojson)
        else:
            return None
        if self.proj is None:
            return g
        else:
            return self._reproject(g, from_proj=kwargs.get('from_proj', 'EPSG:4326'))

    def _reproject(self, geometry, from_proj=None, to_proj=None):
        if from_proj is None:
            from_proj = self._default_proj
        if to_proj is None:
            to_proj = self.proj if self.proj is not None else "EPSG:4326"
        tfm = partial(pyproj.transform, pyproj.Proj(init=from_proj), pyproj.Proj(init=to_proj))
        return ops.transform(tfm, geometry)

    def __getitem__(self, geometry):
        g = shape(geometry)
        assert g in self, "Image does not contain specified geometry {} not in {}".format(g.bounds, self.bounds)
        bounds = ops.transform(self.__geo_transform__.rev, g).bounds
        # NOTE: image is a dask array that implements daskmeta interface (via op)
        result = self[:, bounds[1]:bounds[3], bounds[0]:bounds[2]]
        image = super(DaskImage, self.__class__).__new__(self.__class__,
                                                         result.dask, result.name, result.chunks,
                                                         result.dtype, result.shape)

        image.__geo_interface__ = mapping(g)
        image.__geo_transform__ = self.__geo_transform__ + (bounds[0], bounds[1])
        return image

    def __contains__(self, g):
        geometry = ops.transform(self.__geo_transform__.rev, g)
        img_bounds = box(0, 0, *self.shape[2:0:-1])
        return img_bounds.contains(geometry)


class DaskMetaWrapper(DaskMeta):
    def __init__(self, dask):
        self.da = dask

    @property
    def dask(self):
        return self.da["dask"]

    @property
    def name(self):
        return self.da["name"]

    @property
    def chunks(self):
        return self.da["chunks"]

    @property
    def dtype(self):
        return self.da["dtype"]

    @property
    def shape(self):
        return self.da["shape"]


# Mixin class that defines plotting methods and rgb/ndvi methods
# used as a mixin to provide access to the plot method on
# GeoDaskWrapper images and ipe images
class PlotMixin(object):
    @property
    def _rgb_bands(self):
        return [4, 2, 1]

    @property
    def _ndvi_bands(self):
        return [6, 4]

    def rgb(self, **kwargs):
        data = self._read(self[kwargs.get("bands", self._rgb_bands),...])
        data = np.rollaxis(data.astype(np.float32), 0, 3)
        lims = np.percentile(data, kwargs.get("stretch", [2, 98]), axis=(0, 1))
        for x in xrange(len(data[0,0,:])):
            top = lims[:,x][1]
            bottom = lims[:,x][0]
            data[:,:,x] = (data[:,:,x] - bottom) / float(top - bottom)
        return np.clip(data, 0, 1)

    def ndvi(self, **kwargs):
        data = self._read(self[self._ndvi_bands,...]).astype(np.float32)
        return (data[0,:,:] - data[1,:,:]) / (data[0,:,:] + data[1,:,:])

    def plot(self, spec="rgb", **kwargs):
        if self.shape[0] == 1 or ("bands" in kwargs and len(kwargs["bands"]) == 1):
            if "cmap" in kwargs:
                cmap = kwargs["cmap"]
                del kwargs["cmap"]
            else:
                cmap = "Greys_r"
            self._plot(tfm=self._single_band, cmap=cmap, **kwargs)
        else:
            self._plot(tfm=getattr(self, spec), **kwargs)

    def _plot(self, tfm=lambda x: x, **kwargs):
        assert has_pyplot, "To plot images please install matplotlib"
        assert self.shape[1] and self.shape[-1], "No data to plot, dimensions are invalid {}".format(str(self.shape))

        f, ax1 = plt.subplots(1, figsize=(kwargs.get("w", 10), kwargs.get("h", 10)))
        ax1.axis('off')
        plt.imshow(tfm(**kwargs), interpolation='nearest', cmap=kwargs.get("cmap", None))
        plt.show(block=False)

    def _read(self, data, **kwargs):
        if hasattr(data, 'read'):
            return data.read(**kwargs)
        else:
            return data.compute()

    def _single_band(self, **kwargs):
        return self._read(self[0,:,:], **kwargs)


class GeoDaskWrapper(DaskImage, GeoImage, PlotMixin):
    def __new__(cls, daskmeta, img):
        dm = DaskMetaWrapper(daskmeta)
        self = super(GeoDaskWrapper, cls).create(dm)
        self.__geo_interface__ = img.__geo_interface__
        self.__geo_transform__ = img.__geo_transform__
        return self
