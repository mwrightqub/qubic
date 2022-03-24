import numpy as np
import pickle
import sys
import qubicplus
import pysm3
import pysm3.units as u
from pysm3 import utils
import numpy as np
from qubic import camb_interface as qc
import healpy as hp
import matplotlib.pyplot as plt
from qubic import NamasterLib as nam
import os
import scipy
import random as rd
from getdist import plots, MCSamples
import getdist
import string
from qubic import mcmc
import qubic
from importlib import reload
import pickle
from scipy import constants
import fgbuster
from scipy.optimize import curve_fit
import definitions
from qubic import AnalysisMC as amc


covmap = definitions.get_coverage(0.03, nside=256)
pixok = covmap > 0

def getpkl(path, N, nb_exp, nubreak, iib, ns, fixsync):

    nparam=1

    db=[0, 0.05]#np.linspace(-0.1, 0.1, 7)
    props=[0, 1]
    nub=np.linspace(85, 270, 20)
    if fixsync == 0:
        nsync=''
        nparam+=1
    else:
        nsync='_fixsync'
    #truenub=[100, 150, 200, 250]
    #tabparam = np.zeros((((N*nb_exp, len(props), len(db), nb_param))))
    #tabcl=np.zeros((((N*nb_exp, len(props), len(db), 16))))
    print('N = ', N)    #np.zeros((((len(prop), len(db), N, 1, 16, 4))))
    tabparam = np.zeros((((len(props), len(db), 2*N*nb_exp, nparam))))
    tabcl=np.zeros((((len(props), len(db), N*nb_exp, 1, 8, 4))))
    for k in range(nb_exp):
        #print(k)
        with open(path+'/cls_split{}_nolensing_r0.000_iib{:.0f}_QU{}_truenub{}_{}reals_{}.pkl'.format(ns, iib, nsync, nubreak, N, k+1), 'rb') as f:
            data = pickle.load(f)
        #print(data[2].shape)
        #print(k*N,(k+1)*N)
        tabparam[:, :, 2*k*N:(k+1)*N*2, :]=data[2]
        #print(data[1].shape)
        tabcl[:, :, k*N:(k+1)*N, :, :, :]=data[1]
    leff=data[0]
    return leff, tabcl, tabparam, db



N=int(sys.argv[1])
nb_exp=int(sys.argv[2])
nubreak=int(sys.argv[3])
r=float(sys.argv[4])
iib=int(sys.argv[5])
fixsync=int(sys.argv[6])
N_bands=int(sys.argv[7])
qp_nsub = np.array([1, 1, 1, N_bands, N_bands, N_bands, N_bands, N_bands, N_bands])
name_split=definitions._give_name_splitbands(qp_nsub)
print(name_split)


leff, tabcl, param, db = getpkl('/pbs/home/m/mregnier/sps1/QUBIC+/d0/cls/results', N, nb_exp, nubreak=nubreak, iib=iib, ns=name_split, fixsync=fixsync)

#print(leff)
#print(tabcl, tabcl.shape)
print(param.shape)
print(np.mean(param, axis=2))


from getdist import densities

prop=[0, 1]
maxL=np.zeros(((len(prop), N*nb_exp, len(db))))
rlim68=np.zeros(((len(prop), N*nb_exp, len(db))))
rlim95=np.zeros(((len(prop), N*nb_exp, len(db))))



for i in range(len(prop)):
    for j in range(len(db)):
        print()
        print(i, j)
        print()
        cls=tabcl[i, j, :, :, :, :].copy()
        new_cl = np.moveaxis(cls, [1, 2, 3], [3, 1, 2])
        covbin, _ = amc.get_covcorr_patch(new_cl, stokesjoint=True, doplot=False)
        #print(covbin)

        ml, r68, r95 = definitions.get_like_onereals(leff, new_cl, db, covmap, covbin=None)

        #print(ml)

        maxL[i, :, j]=ml.copy()
        rlim68[i, :, j]=r68[1].copy()
        rlim95[i, :, j]=r95[1].copy()

print('\\\\\\\ Results ')
print()
print('Mean MaxL ->', np.mean(maxL, axis=1))
print()
#print('Bias -> ', np.mean(maxL, axis=1)-np.mean(maxL, axis=1)[:, 0])
print()
print('Mean rlim68 ->', np.mean(rlim68, axis=1))
print()
print('Mean rlim95 ->', np.mean(rlim95, axis=1))

if fixsync == 0:
    nsync=''
else:
    nsync='_fixsync'
#maxLbi, rlim68_bi, rlim95_bi = get_like_onereals(leff, clsBBbi, db, covmap)

pickle.dump([leff, maxL, rlim68, rlim95, param, tabcl, db, sys.argv], open('/pbs/home/m/mregnier/sps1/QUBIC+/d0/cls/r{:.3f}_split{}_nolensing_param{}_cls_nubreak{}_iib{:.0f}_{:.0f}reals.pkl'.format(r, name_split, nsync, nubreak, iib, N*nb_exp), "wb"))
