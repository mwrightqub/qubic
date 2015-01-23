# coding: utf-8
from __future__ import division

import healpy as hp
import numexpr as ne
import numpy as np
from pyoperators import (
    Cartesian2SphericalOperator, DenseBlockDiagonalOperator, IdentityOperator,
    HomothetyOperator, ReshapeOperator, Rotation2dOperator, Rotation3dOperator,
    Spherical2CartesianOperator)
from pyoperators.utils import pool_threading
from pyoperators.utils.ufuncs import abs2
from pysimulators import (
    ConvolutionTruncatedExponentialOperator, Instrument, Layout,
    ProjectionOperator)
from pysimulators.geometry import surface_simple_polygon
from pysimulators.interfaces.healpy import Cartesian2HealpixOperator
from pysimulators.sparse import (
    FSRMatrix, FSRRotation2dMatrix, FSRRotation3dMatrix)
from scipy.constants import c, pi
from . import _flib as flib
from .beams import GaussianBeam
from .calibration import QubicCalibration
from .utils import _compress_mask

__all__ = ['QubicInstrument',
           'SimpleInstrument']


class SimpleInstrument(Instrument):
    """
    The SimpleInstrument class. Classical imager with a well-behaved
    single-peak beam.

    """
    def __init__(self, calibration=None, detector_fknee=0, detector_fslope=1,
                 detector_ncorr=10, detector_ngrids=2, detector_sigma=10,
                 detector_tau=0.01, synthbeam_dtype=np.float32, **keywords):
        """
        Parameters
        ----------
        calibration : QubicCalibration
            The calibration tree.
        detector_fknee : array-like
            The detector 1/f knee frequency in Hertz.
        detector_fslope : array-like
            The detector 1/f slope index.
        detector_ncorr : int
            The detector 1/f correlation length.
        detector_ngrids : int, optional
            Number of detector grids.
        detector_sigma : array-like
            The standard deviation of the detector white noise component.
        detector_tau : array-like
            The detector time constants in seconds.
        synthbeam_dtype : dtype, optional
            The data type for the synthetic beams (default: float32).
            It is the dtype used to store the values of the pointing matrix.

        """
        if calibration is None:
            calibration = QubicCalibration()
        self.calibration = calibration
        layout = self._get_detector_layout(
            detector_ngrids, detector_sigma, detector_fknee, detector_fslope,
            detector_ncorr, detector_tau)
        Instrument.__init__(self, 'QUBIC', layout)
        self._init_optics(**keywords)
        self._init_synthbeam(synthbeam_dtype)

    def _get_detector_layout(self, ngrids, sigma, fknee, fslope, ncorr,
                             tau):
        shape, vertex, removed, index, quadrant = \
            self.calibration.get('detarray')
        if ngrids == 2:
            shape = (2,) + shape
            vertex = np.array([vertex, vertex])
            removed = np.array([removed, removed])
            index = np.array([index, index + np.max(index) + 1], index.dtype)
            quadrant = np.array([quadrant, quadrant + 4], quadrant.dtype)
        focal_length = self.calibration.get('optics')['focal length']

        vertex = np.concatenate(
            [vertex, np.full_like(vertex[..., :1], -focal_length)], -1)
        center = np.mean(vertex, axis=-2)
        # assume all detectors have the same area
        theta = np.pi - np.arctan2(
            np.sqrt(np.sum(center[..., :2]**2, axis=-1)), focal_length)
        phi = np.arctan2(center[..., 1], center[..., 0])

        layout = Layout(
            shape, vertex=vertex, selection=~removed, ordering=index,
            quadrant=quadrant, sigma=sigma, fknee=fknee, fslope=fslope,
            tau=tau, theta=theta, phi=phi)
        layout.area = surface_simple_polygon(layout.vertex[0, :, :2])
        layout.ncorr = ncorr
        layout.ngrids = ngrids
        return layout

    def _init_optics(self, **keywords):
        class Optics(object):
            pass
        optics = Optics()
        optics.focal_length = self.calibration.get('optics')['focal length']
        self.optics = optics

    def _init_synthbeam(self, dtype):
        class SyntheticBeam(object):
            pass
        sb = SyntheticBeam()
        sb.dtype = np.dtype(dtype)
        self.synthbeam = sb

    def __str__(self):
        state = [('ngrids', self.detector.ngrids),
                 ('selection', _compress_mask(~self.detector.all.removed))]
        return 'Instrument:\n' + \
               '\n'.join(['    ' + a + ': ' + repr(v) for a, v in state]) + \
               '\n\nCalibration:\n' + '\n'. \
               join('    ' + l for l in str(self.calibration).splitlines())

    __repr__ = __str__

    def get_noise(self, sampling, out=None):
        """
        Return a noisy timeline.

        """
        return Instrument.get_noise(
            self, sampling, sigma=self.detector.sigma,
            fknee=self.detector.fknee, fslope=self.detector.fslope, out=out)

    def get_detector_response_operator(self, sampling, tau=None):
        """
        Return the operator for the bolometer responses.

        """
        if tau is None:
            tau = self.detector.tau
        sampling_period = sampling.period
        shapein = len(self), len(sampling)
        if sampling_period == 0:
            return IdentityOperator(shapein)
        return ConvolutionTruncatedExponentialOperator(
            tau / sampling_period, shapein=shapein)

    def get_hwp_operator(self, sampling, scene):
        """
        Return the rotation matrix for the half-wave plate.

        """
        shape = (len(self), len(sampling))
        if scene.kind == 'I':
            return IdentityOperator(shapein=shape)
        if scene.kind == 'QU':
            return Rotation2dOperator(-4 * sampling.angle_hwp,
                                      degrees=True, shapein=shape + (2,))
        return Rotation3dOperator('X', -4 * sampling.angle_hwp,
                                  degrees=True, shapein=shape + (3,))

    def get_invntt_operator(self, sampling):
        """
        Return the inverse time-time noise correlation matrix as an Operator.

        """
        return Instrument.get_invntt_operator(
            self, sampling, sigma=self.detector.sigma,
            fknee=self.detector.fknee, fslope=self.detector.fslope,
            ncorr=self.detector.ncorr)

    def get_polarizer_operator(self, sampling, scene):
        """
        Return operator for the polarizer grid.

        """
        if scene.kind == 'I':
            return HomothetyOperator(1 / self.detector.ngrids)

        if self.detector.ngrids == 1:
            raise ValueError(
                'Polarized input not handled by a single detector grid.')

        nd = len(self)
        nt = len(sampling)
        grid = self.detector.quadrant // 4
        z = np.zeros(nd)
        data = np.array([z + 0.5, 0.5 - grid, z]).T[:, None, None, :]
        return ReshapeOperator((nd, nt, 1), (nd, nt)) * \
            DenseBlockDiagonalOperator(data, shapein=(nd, nt, 3))

    def get_projection_operator(self, sampling, scene, verbose=True):
        """
        Return the peak sampling operator.

        Parameters
        ----------
        sampling : QubicSampling
            The pointing information.
        scene : QubicScene
            The observed scene.
        verbose : bool, optional
            If true, display information about the memory allocation.

        """
        rotation = sampling.cartesian_galactic2instrument
        dtype = self.synthbeam.dtype
        ndetectors = len(self)
        ntimes = len(sampling)
        nside = scene.nside

        theta, phi, vals = self._peak_angles(scene)
        ncolmax = theta.shape[-1]
        thetaphi = _pack_vector(theta, phi)  # (ndetectors, ncolmax, 2)
        direction = Spherical2CartesianOperator('zenith,azimuth')(thetaphi)
        e_nf = direction[:, None, :, :]
        if nside > 8192:
            dtype_index = np.dtype(np.int64)
        else:
            dtype_index = np.dtype(np.int32)

        cls = {'I': FSRMatrix,
               'QU': FSRRotation2dMatrix,
               'IQU': FSRRotation3dMatrix}[scene.kind]
        ndims = len(scene.kind)
        s = cls((ndetectors * ntimes * ndims, 12 * nside**2 * ndims),
                ncolmax=ncolmax, dtype=dtype, dtype_index=dtype_index,
                verbose=verbose)

        index = s.data.index.reshape((ndetectors, ntimes, ncolmax))

        def func_thread(i):
            # e_nf[i] shape: (1, ncolmax, 3)
            # e_ni shape: (ntimes, ncolmax, 3)
            e_ni = rotation.T(e_nf[i].swapaxes(0, 1)).swapaxes(0, 1)
            index[i] = Cartesian2HealpixOperator(nside)(e_ni)

        with pool_threading() as pool:
            pool.map(func_thread, xrange(ndetectors))

        if scene.kind == 'I':
            value = s.data.value.reshape(ndetectors, ntimes, ncolmax)
            value[...] = vals[:, None, :]
            shapeout = (ndetectors, ntimes)
        else:
            if str(dtype_index) not in ('int32', 'int64') or \
               str(dtype) not in ('float32', 'float64'):
                raise TypeError(
                    'The projection matrix cannot be created with types: {0} a'
                    'nd {1}.'.format(dtype_index, dtype))
            func = 'matrix_rot{0}d_i{1}_r{2}'.format(
                ndims, dtype_index.itemsize, dtype.itemsize)
            getattr(flib.polarization, func)(
                rotation.data.T, direction.T, s.data.ravel().view(np.int8),
                vals.T)

            if scene.kind == 'QU':
                shapeout = (ndetectors, ntimes, 2)
            else:
                shapeout = (ndetectors, ntimes, 3)

        return ProjectionOperator(s, shapeout=shapeout)

    def _peak_angles(self, scene):
        """
        Return the spherical coordinates (theta, phi) of the beam peaks,
        in radians.

        """
        theta = -self.detector.theta[:, None]
        phi = self.detector.phi[:, None] + np.pi
        val = np.ones_like(theta)
        return theta, phi, val


class QubicInstrument(SimpleInstrument):
    """
    The QubicInstrument class. It represents the instrument setup.

    """
    def __init__(self, calibration=None, detector_fknee=0, detector_fslope=1,
                 detector_ncorr=10, detector_ngrids=2, detector_sigma=10,
                 detector_tau=0.01, primary_beam=None, secondary_beam=None,
                 synthbeam_dtype=np.float32, synthbeam_fraction=0.99,
                 **keywords):
        """
        Parameters
        ----------
        calibration : QubicCalibration
            The calibration tree.
        detector_fknee : array-like
            The detector 1/f knee frequency in Hertz.
        detector_fslope : array-like
            The detector 1/f slope index.
        detector_ncorr : int
            The detector 1/f correlation length.
        detector_ngrids : int, optional
            Number of detector grids.
        detector_sigma : array-like
            The standard deviation of the detector white noise component.
        detector_tau : array-like
            The detector time constants in seconds.
        primary_beam : function f(theta [rad], phi [rad])
            The primary beam transmission function.
        secondary_beam : function f(theta [rad], phi [rad])
            The secondary beam transmission function.
        synthbeam_dtype : dtype, optional
            The data type for the synthetic beams (default: float32).
            It is the dtype used to store the values of the pointing matrix.
        synthbeam_fraction: float, optional
            The fraction of significant peaks retained for the computation
            of the synthetic beam.

        """
        if calibration is None:
            calibration = QubicCalibration()
        SimpleInstrument.__init__(
            self, calibration, detector_fknee, detector_fslope, detector_ncorr,
            detector_ngrids, detector_sigma, detector_tau, synthbeam_dtype,
            **keywords)
        self._init_beams(primary_beam, secondary_beam)
        self._init_horns()
        self.synthbeam.fraction = synthbeam_fraction
        self.synthbeam.kmax = 8  # all peaks are considered

    def _init_beams(self, primary, secondary):
        if primary is None:
            primary = GaussianBeam(self.calibration.get('primbeam'))
        self.primary_beam = primary
        if secondary is None:
            secondary = GaussianBeam(self.calibration.get('primbeam'),
                                     backward=True)
        self.secondary_beam = secondary

    def _init_horns(self):
        self.horn = self.calibration.get('hornarray')

    def __str__(self):
        state = [('synthbeam_fraction', self.synthbeam.fraction)]
        out = SimpleInstrument.__str__(self)
        index = out.index('\nCalibration')
        return out[:index] + \
               '\n'.join(['    {0}: {1!r}'.format(*_) for _ in state]) + \
               out[index:]

    __repr__ = __str__

    def _peak_angles(self, scene):
        fraction = self.synthbeam.fraction
        theta, phi = self._peak_angles_kmax(scene, self.synthbeam.kmax)
        val = np.array(self.primary_beam(theta, phi), dtype=float, copy=False)
        val[~np.isfinite(val)] = 0
        val /= np.sum(val, axis=-1)[:, None]
        index = _argsort_reverse(val)
        theta = theta[index]
        phi = phi[index]
        val = val[index]
        cumval = np.cumsum(val, axis=-1)
        imaxs = np.argmax(cumval >= fraction, axis=-1) + 1
        imax = max(imaxs)

        # slice initial arrays to discard the non-significant peaks
        theta = theta[:, :imax]
        phi = phi[:, :imax]
        val = val[:, :imax]

        # remove additional per-detector non-significant peaks
        # and remove potential NaN in theta, phi
        for idet, imax_ in enumerate(imaxs):
            val[idet, imax_:] = 0
            theta[idet, imax_:] = pi / 2 #XXX 0 leads to NaN
            phi[idet, imax_:] = 0
        val /= np.sum(val, axis=-1)[:, None]
        val *= self.secondary_beam(self.detector.theta, 0.)
        return theta, phi, val

    def _peak_angles_kmax(self, scene, kmax):
        """
        Return the spherical coordinates (theta, phi) of the beam peaks,
        in radians.

        Parameters
        ----------
        kmax : int, optional
            The diffraction order above which the peaks are ignored.
            For instance, a value of kmax=2 will model the synthetic beam by
            (2 * kmax + 1)**2 = 25 peaks and a value of kmax=0 will only sample
            the central peak.
        """
        ndetector = len(self)
        center = self.detector.center
        lmbda = c / scene.nu
        dx = self.horn.spacing
        detvec = np.vstack([-center[..., 0],
                            -center[..., 1],
                            np.zeros(ndetector) + self.optics.focal_length]).T
        detvec.T[...] /= np.sqrt(np.sum(detvec**2, axis=1))

        kx, ky = np.mgrid[-kmax:kmax+1, -kmax:kmax+1]
        nx = detvec[:, 0, np.newaxis] - lmbda * kx.ravel() / dx
        ny = detvec[:, 1, np.newaxis] - lmbda * ky.ravel() / dx
        local_dict = {'nx': nx, 'ny': ny}
        theta = ne.evaluate('arcsin(sqrt(nx**2 + ny**2))',
                            local_dict=local_dict)
        phi = ne.evaluate('arctan2(ny, nx)', local_dict=local_dict)
        return theta, phi

    def get_response_A(self, scene, x=None, y=None, area=1):
        """
        Phase and transmission from the switches to the focal plane.

        Parameters
        ----------
        scene : QubicScene
            The scene.
        x : array-like, optional
            The X-coordinate in the focal plane where the response is computed,
            in meters. If not provided, the detector central positions are
            assumed.
        y : array-like, optional
            The Y-coordinate in the focal plane where the response is computed,
            in meters. If not provided, the detector central positions are
            assumed.
        area : array-like, optional
            The integration area, in m^2.

        Returns
        -------
        out : complex array of shape (#positions, #horns)
            The phase and transmission from the horns to the focal plane.

        """
        f = self.optics.focal_length
        if x is None and y is None:
            uvec = self.detector.center
            area = self.detector.area
        elif x is not None and y is not None:
            x, y, area = [np.array(_, dtype=float, copy=False)
                          for _ in x, y, area]
            x, y, area = np.broadcast_arrays(x, y, area)
            uvec = np.array([x, y, np.full_like(x, -f)])
            # roll first axis to last
            uvec = np.rollaxis(uvec[..., None], 0, -1)[..., 0]
        else:
            raise ValueError('Input x or y not specified.')
        uvec /= np.sqrt(np.sum(uvec**2, axis=-1))[..., None]
        thetaphi = Cartesian2SphericalOperator('zenith,azimuth')(uvec)
        sr = -area / f**2 * np.cos(thetaphi[..., 0])**3
        tr = np.sqrt(self.secondary_beam(thetaphi[..., 0], thetaphi[..., 1]) *
                     sr / self.secondary_beam.solid_angle)[..., None]
        const = 2j * pi * scene.nu / c
        product = np.dot(uvec, self.horn[self.horn.open].center.T)
        return ne.evaluate('tr * exp(const * product)')

    def get_response_B(self, scene, theta, phi, power=1):
        """
        Phase and transmission from the source to the switches.

        Parameters
        ----------
        scene : QubicScene
            The scene.
        theta : array-like
            The source zenith angle [rad].
        phi : array-like
            The source azimuthal angle [rad].
        power : array-like
            The source power [W].

        Returns
        -------
        out : complex array of shape (#horns, #sources)
            The phase and transmission from the source to the horns.

        """
        shape = np.broadcast(theta, phi, power).shape
        theta, phi, power = [np.ravel(_) for _ in theta, phi, power]
        uvec = hp.ang2vec(theta, phi)
        source_E = np.sqrt(power * self.primary_beam(theta, phi))
        const = 2j * pi * scene.nu / c
        product = np.dot(self.horn[self.horn.open].center, uvec.T)
        out = ne.evaluate('source_E * exp(const * product)')
        return out.reshape((-1,) + shape)

    def get_response(self, scene, theta, phi, power=1, x=None, y=None,
                     area=1):
        """
        Return the electric field created by sources at specified angles
        on specified locations of the focal planes.

        Parameters
        ----------
        scene : QubicScene
            The scene.
        theta : array-like
            The source zenith angle [rad].
        phi : array-like
            The source azimuthal angle [rad].
        power : array-like
            The source power [W].
        x : array-like, optional
            The X-coordinate in the focal plane where the response is computed,
            in meters. If not provided, the detector central positions are
            assumed.
        y : array-like, optional
            The Y-coordinate in the focal plane where the response is computed,
            in meters. If not provided, the detector central positions are
            assumed.
        area : array-like, optional
            The integration area, in m^2.

        Returns
        -------
        out : array of shape (#positions, #sources)
            The electric field on the specified focal plane positions.

        """
        A = self.get_response_A(scene, x, y, area)
        B = self.get_response_B(scene, theta, phi, power)
        E = np.dot(A, B.reshape((B.shape[0], -1))).reshape(
            A.shape[:-1] + B.shape[1:])
        return E

    def get_synthbeam_healpix_from_position(self, scene, x, y, theta_max=45):

        """
        Return the monochromatic synthetic beam for a specified location
        on the focal plane.

        Parameters
        ----------
        scene : QubicScene
            The scene.
        x : array-like, optional
            The X-coordinate in the focal plane where the response is computed,
            in meters. If not provided, the detector central positions are
            assumed.
        y : array-like, optional
            The Y-coordinate in the focal plane where the response is computed,
            in meters. If not provided, the detector central positions are
            assumed.
        theta_max : float, optional
            The maximum zenithal angle above which the synthetic beam is
            assumed to be zero, in radians.

        """
        MAX_MEMORY_B = 1e9
        theta, phi = hp.pix2ang(scene.nside, scene.index)
        index = np.where(theta <= np.radians(theta_max))[0]
        nhorn = len(self.horn.open)
        npix = len(index)
        nbytes_B = npix * nhorn * 24
        ngroup = np.ceil(nbytes_B / MAX_MEMORY_B)
        if x is None and y is None:
            shape = ()
        else:
            shape = np.broadcast(x, y).shape
        out = np.zeros(shape + (len(scene),), dtype=self.synthbeam.dtype)
        for s in split(npix, ngroup):
            index_ = index[s]
            sb = self.get_response(scene, theta[index_], phi[index_], power=1,
                                   x=x, y=y, area=1)
            out[..., index_] = abs2(sb, dtype=self.synthbeam.dtype)
        out /= np.sum(out)
        return out


def _argsort_reverse(a, axis=-1):
    i = list(np.ogrid[[slice(x) for x in a.shape]])
    i[axis] = a.argsort(axis)[:, ::-1]
    return i


def _pack_vector(*args):
    shape = np.broadcast(*args).shape
    out = np.empty(shape + (len(args),))
    for i, arg in enumerate(args):
        out[..., i] = arg
    return out
