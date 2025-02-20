from csr2d.deposit import split_particles, deposit_particles, histogram_cic_2d
from csr2d.central_difference import central_difference_z
from csr2d.core import psi_s, psi_x, psi_x_where_x_equals_zero

import numpy as np

from scipy.signal import savgol_filter
from scipy.interpolate import RectBivariateSpline
from scipy.signal import convolve2d, fftconvolve, oaconvolve

import scipy.constants

mec2 = scipy.constants.value("electron mass energy equivalent in MeV") * 1e6
c_light = scipy.constants.c
e_charge = scipy.constants.e
r_e = scipy.constants.value("classical electron radius")

import time


def csr2d_kick_calc(
    z_b,
    x_b,
    weight,
    *,
    gamma=None,
    rho=None,
    nz=100,
    nx=100,
    xlim=None,
    zlim=None,
    reuse_psi_grids=False,
    psi_s_grid_old=None,
    psi_x_grid_old=None,
    map_f=map,
    species="electron",
    debug=False,
):
    """
    
    Calculates the 2D CSR kick on a set of particles with positions `z_b`, `x_b` and charges `charges`.
    
    
    Parameters
    ----------
    z_b : np.array
        Bunch z coordinates in [m]

    x_b : np.array
        Bunch x coordinates in [m]
  
    weight : np.array
        weight array (positive only) in [C]
        This should sum to the total charge in the bunch
        
    gamma : float
        Relativistic gamma
        
    rho : float
        bending radius in [m]
        if neagtive, particles with a positive x coordinate is on the inner side of the magnet
        
    nz : int
        number of z grid points
        
    nx : int
        number of x grid points        
    
    zlim : floats (min, max) or None
        z grid limits in [m]
        
    xlim : floats (min, max) or None  
        x grid limits in [m]
        
    map_f : map function for creating potential grids.
            Examples:
                map (default)
                executor.map
    
    species : str
        Particle species. Currently required to be 'electron'
    
    debug: bool
        If True, returns the computational grids. 
        Default: False
        
              
    Returns
    -------
    dict with:
    
        ddelta_ds : np.array
            relative z momentum kick [1/m]
            
        dxp_ds : np.array
            relative x momentum kick [1/m]
        
    
        
    """
    assert species == "electron", "TODO: support species {species}"
    # assert np.sign(rho) == 1, 'TODO: negative rho'

    rho_sign = np.sign(rho)
    if rho_sign == -1:
        rho = -rho
        x_b = -x_b  # flip the beam x coordinate

    # Grid setup
    if zlim:
        zmin = zlim[0]
        zmax = zlim[1]
    else:
        zmin = z_b.min()
        zmax = z_b.max()

    if xlim:
        xmin = xlim[0]
        xmax = xlim[1]
    else:
        xmin = x_b.min()
        xmax = x_b.max()

    dz = (zmax - zmin) / (nz - 1)
    dx = (xmax - xmin) / (nx - 1)

    # Charge deposition

    # Old method
    # zx_positions = np.stack((z_b, x_b)).T
    # indexes, contrib = split_particles(zx_positions, charges, mins, maxs, sizes)
    # t1 = time.time();
    # charge_grid = deposit_particles(Np, sizes, indexes, contrib)
    # t2 = time.time();

    # Remi's fast code
    t1 = time.time()
    charge_grid = histogram_cic_2d(z_b, x_b, weight, nz, zmin, zmax, nx, xmin, xmax)

    if debug:
        t2 = time.time()
        print("Depositing particles takes:", t2 - t1, "s")

    # Normalize the grid so its integral is unity
    norm = np.sum(charge_grid) * dz * dx
    lambda_grid = charge_grid / norm

    # Apply savgol filter
    lambda_grid_filtered = np.array([savgol_filter(lambda_grid[:, i], 13, 2) for i in np.arange(nx)]).T

    # Differentiation in z
    lambda_grid_filtered_prime = central_difference_z(lambda_grid_filtered, nz, nx, dz, order=1)

    # Grid axis vectors
    zvec = np.linspace(zmin, zmax, nz)
    xvec = np.linspace(xmin, xmax, nx)

    beta = np.sqrt(1 - 1 / gamma ** 2)

    t3 = time.time()

    if reuse_psi_grids == True:
        psi_s_grid = psi_s_grid_old
        psi_x_grid = psi_x_grid_old

    else:
        # Creating the potential grids
        #zvec2 = np.linspace(2 * zmin, 2 * zmax, 2 * nz)
        #xvec2 = np.linspace(2 * xmin, 2 * xmax, 2 * nx)
        zvec2 = np.arange(-nz,nz,1)*dz # center = 0 is at [nz]
        xvec2 = np.arange(-nx,nx,1)*dx # center = 0 is at [nx]
        zm2, xm2 = np.meshgrid(zvec2, xvec2, indexing="ij")

        beta_grid = beta * np.ones(zm2.shape)

        # Map (possibly parallel)
        temp = map_f(psi_s, zm2 / 2 / rho, xm2 / rho, beta_grid)
        psi_s_grid = np.array(list(temp))
        temp2 = map_f(psi_x, zm2 / 2 / rho, xm2 / rho, beta_grid)
        psi_x_grid = np.array(list(temp2))
        
        # Replacing the fake zeros along the x_axis ( due to singularity) with averaged value from the nearby grid
        psi_x_grid[:,nx] = psi_x_where_x_equals_zero(zvec2, dx/rho, beta)

    if debug:
        t4 = time.time()
        print("Computing potential grids take:", t4 - t3, "s")

    # Compute the wake via 2d convolution
    conv_s = oaconvolve(lambda_grid_filtered_prime, psi_s_grid, mode="same")
    conv_x = oaconvolve(lambda_grid_filtered_prime, psi_x_grid, mode="same")

    if debug:
        t5 = time.time()
        print("Convolution takes:", t5 - t4, "s")

    Ws_grid = (beta ** 2 / rho) * (conv_s) * (dz * dx)
    Wx_grid = (beta ** 2 / rho) * (conv_x) * (dz * dx)

    # Interpolate Ws and Wx everywhere within the grid
    Ws_interp = RectBivariateSpline(zvec, xvec, Ws_grid)
    Wx_interp = RectBivariateSpline(zvec, xvec, Wx_grid)

    # Overall factor
    Nb = np.sum(weight) / e_charge
    kick_factor = r_e * Nb / gamma  # m

    # Calculate the kicks at the particle locations
    delta_kick = kick_factor * Ws_interp.ev(z_b, x_b)
    xp_kick = kick_factor * Wx_interp.ev(z_b, x_b)
    
    if debug:
        t6 = time.time()
        print("Interpolation takes:", t6 - t5, "s")        

    if rho_sign == -1:
        xp_kick = -xp_kick

    result = {"ddelta_ds": delta_kick, "dxp_ds": xp_kick}

    if debug:
        result.update(
            {
                "zvec": zvec,
                "xvec": xvec,
                "zvec2": zvec2,
                "xvec2": xvec2,
                "Ws_grid": Ws_grid,
                "Wx_grid": Wx_grid,
                "psi_s_grid": psi_s_grid,
                "psi_x_grid": psi_x_grid,
                "charge_grid": charge_grid,
                "lambda_grid_filtered_prime": lambda_grid_filtered_prime,
            }
        )

    return result


def csr1d_steady_state_kick_calc(z, weights, *, nz=100, rho=1, species="electron"):

    """

    Steady State CSR 1D model kick calc

    
    Parameters
    ----------
    z : np.array
        Bunch z coordinates in [m]    
        
    weights : np.array
        weight array (positive only) in [C]
        This should sum to the total charge in the bunch        
        
    nz : int
        number of z grid points        
        
    rho : float
        bending radius in [m]        
        
    species : str
        Particle species. Currently required to be 'electron'   
        
    Returns
    -------
    dict with:
    
        denergy_ds : np.array
            energy kick for each particle [eV/m]
            
        wake : np.array
            wake array that kicks were interpolated on
            
        zvec : np.array
            z coordinates for wake array
    
    """

    assert species == "electron", f"TODO: support species {species}"

    # Density
    H, edges = np.histogram(z, weights=weights, bins=nz)
    zmin, zmax = edges[0], edges[-1]
    dz = (zmax - zmin) / (nz - 1)

    zvec = np.linspace(zmin, zmax, nz)  # Sloppy with bin centers

    Qtot = np.sum(weights)
    density = H / dz / Qtot

    # Density derivative
    densityp = np.gradient(density) / dz
    densityp_filtered = savgol_filter(densityp, 13, 2)

    # Green function
    zi = np.arange(0, zmax - zmin, dz)
    factor = (
        -3 ** (2 / 3) * Qtot / e_charge * r_e * mec2 * rho ** (-2 / 3)
    )  # factor for denergy/dz [eV/m]
    # factor = -3**(2/3) * Qtot/e_charge * r_e * rho**(-2/3) / gamma  # factor for ddelta/ds [1/m]
    green = factor * np.diff(zi ** (2 / 3))

    # Convolve to get wake
    wake = np.convolve(densityp_filtered, green, mode="full")[0 : len(zvec)]

    # Interpolate to get the kicks
    delta_kick = np.interp(z, zvec, wake)

    return {"denergy_ds": delta_kick, "zvec": zvec, "wake": wake}
