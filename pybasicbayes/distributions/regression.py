from __future__ import division
__all__ = ['Regression', 'RegressionNonconj', 'ARDRegression']

import numpy as np
from numpy import newaxis as na

from pybasicbayes.abstractions import GibbsSampling, MaxLikelihood
from pybasicbayes.util.stats import sample_gaussian, sample_mniw, \
    sample_invwishart, getdatasize
from pybasicbayes.util.general import blockarray, inv_psd, cumsum, \
    all_none, any_none


class Regression(GibbsSampling, MaxLikelihood):
    def __init__(
            self, nu_0=None,S_0=None,M_0=None,K_0=None,
            affine=False,
            A=None,sigma=None):
        self.affine = affine

        self.A = A
        self.sigma = sigma

        have_hypers = not any_none(nu_0,S_0,M_0,K_0)

        if have_hypers:
            self.natural_hypparam = self._standard_to_natural(nu_0,S_0,M_0,K_0)

        if A is sigma is None and have_hypers:
            self.resample()  # initialize from prior

    @property
    def parameters(self):
        return (self.A, self.sigma)

    @parameters.setter
    def parameters(self, A_sigma_tuple):
        (A,sigma) = A_sigma_tuple
        self.A = A
        self.sigma = sigma

    @property
    def D_in(self):
        # NOTE: D_in includes the extra affine coordinate
        mat = self.A if self.A is not None else self.natural_hypparam[1]
        return mat.shape[1]

    @property
    def D_out(self):
        mat = self.A if self.A is not None else self.natural_hypparam[1]
        return mat.shape[0]

    ### converting between natural and standard parameters

    @staticmethod
    def _standard_to_natural(nu,S,M,K):
        Kinv = inv_psd(K)
        A = S + M.dot(Kinv).dot(M.T)
        B = M.dot(Kinv)
        C = Kinv
        d = nu
        return np.array([A,B,C,d])

    @staticmethod
    def _natural_to_standard(natparam):
        A,B,C,d = natparam
        nu = d
        Kinv = C
        K = inv_psd(Kinv)
        M = B.dot(K)
        S = A - B.dot(K).dot(B.T)

        # numerical padding here...
        K += 1e-8*np.eye(K.shape[0])
        assert np.all(0 < np.linalg.eigvalsh(S))
        assert np.all(0 < np.linalg.eigvalsh(K))

        return nu, S, M, K

    ### getting statistics

    def _get_statistics(self,data):
        if isinstance(data,list):
            return sum((self._get_statistics(d) for d in data),
                       self._empty_statistics())
        else:
            data = data[~np.isnan(data).any(1)]
            n, D = data.shape[0], self.D_out

            statmat = data.T.dot(data)
            xxT, yxT, yyT = \
                statmat[:-D,:-D], statmat[-D:,:-D], statmat[-D:,-D:]

            if self.affine:
                xy = data.sum(0)
                x, y = xy[:-D], xy[-D:]
                xxT = blockarray([[xxT,x[:,na]],[x[na,:],np.atleast_2d(n)]])
                yxT = np.hstack((yxT,y[:,na]))

            return np.array([yyT, yxT, xxT, n])

    def _get_weighted_statistics(self,data,weights):
        if isinstance(data,list):
            return sum((self._get_statistics(d) for d in data),
                       self._empty_statistics())
        else:
            gi = ~np.isnan(data).any(1)
            data, weights = data[gi], weights[gi]
            n, D = weights.sum(), self.D_out

            statmat = data.T.dot(weights[:,na]*data)
            xxT, yxT, yyT = \
                statmat[:-D,:-D], statmat[-D:,:-D], statmat[-D:,-D:]

            if self.affine:
                xy = weights.dot(data)
                x, y = xy[:-D], xy[-D:]
                xxT = blockarray([[xxT,x[:,na]],[x[na,:],np.atleast_2d(n)]])
                yxT = np.hstack((yxT,y[:,na]))

            return np.array([yyT, yxT, xxT, n])

    def _empty_statistics(self):
        D_in, D_out = self.D_in, self.D_out
        return np.array(
            [np.zeros((D_out,D_out)), np.zeros((D_out,D_in)),
             np.zeros((D_in,D_in)),0])

    ### distribution

    def log_likelihood(self,xy):
        A, sigma, D = self.A, self.sigma, self.D_out
        x, y = xy[:,:-D], xy[:,-D:]

        if self.affine:
            A, b = A[:,:-1], A[:,-1]

        sigma_inv, L = inv_psd(sigma, return_chol=True)
        parammat = -1./2 * blockarray([
            [A.T.dot(sigma_inv).dot(A), -A.T.dot(sigma_inv)],
            [-sigma_inv.dot(A), sigma_inv]
        ])
        out = np.einsum('ni,ni->n',xy.dot(parammat),xy)
        out -= D/2*np.log(2*np.pi) + np.log(np.diag(L)).sum()

        if self.affine:
            out += y.dot(sigma_inv).dot(b)
            out -= x.dot(A.T).dot(sigma_inv).dot(b)
            out -= 1./2*b.dot(sigma_inv).dot(b)

        return out

    def rvs(self,x=None,size=1,return_xy=True):
        A, sigma = self.A, self.sigma

        if self.affine:
            A, b = A[:,:-1], A[:,-1]

        x = np.random.normal(size=(size,A.shape[1])) if x is None else x
        y = x.dot(A.T) + np.random.normal(size=(x.shape[0],self.D_out))\
            .dot(np.linalg.cholesky(sigma).T)

        if self.affine:
            y += b.T

        return np.hstack((x,y)) if return_xy else y

    ### Gibbs sampling

    def resample(self,data=[],stats=None):
        stats = self._get_statistics(data) if stats is None else stats
        self.A, self.sigma = sample_mniw(
            *self._natural_to_standard(self.natural_hypparam + stats))

    ### Max likelihood

    def max_likelihood(self,data,weights=None,stats=None):
        if stats is None:
            stats = self._get_statistics(data) if weights is None \
                else self._get_weighted_statistics(data,weights)

        yyT, yxT, xxT, n = stats

        if n > 0:
            try:
                self.A = np.linalg.solve(xxT, yxT.T).T
                self.sigma = (yyT - self.A.dot(yxT.T))/n

                def symmetrize(A):
                    return (A + A.T)/2.
                self.sigma = 1e-10*np.eye(self.D_out) \
                    + symmetrize(self.sigma)  # numerical
            except np.linalg.LinAlgError:
                self.broken = True
        else:
            self.broken = True

        assert np.allclose(self.sigma,self.sigma.T)
        assert np.all(np.linalg.eigvalsh(self.sigma) > 0.)

        return self


class RegressionNonconj(Regression):
    def __init__(self, M_0, Sigma_0, nu_0, S_0,
                 A=None, sigma=None, affine=False, niter=10):
        self.A = A
        self.sigma = sigma
        self.affine = affine

        self.h_0 = np.linalg.solve(Sigma_0, M_0.ravel()).reshape(M_0.shape)
        self.J_0 = np.linalg.inv(Sigma_0)
        self.nu_0 = nu_0
        self.S_0 = S_0

        self.niter = niter

        if all_none(A,sigma):
            self.resample()  # initialize from prior

    ### Gibbs

    def resample(self,data=[],niter=None):
        niter = niter if niter else self.niter
        if getdatasize(data) == 0:
            self.A = sample_gaussian(J=self.J_0,h=self.h_0.ravel())\
                .reshape(self.h_0.shape)
            self.sigma = sample_invwishart(self.S_0,self.nu_0)
        else:
            yyT, yxT, xxT, n = self._get_statistics(data)
            for itr in xrange(niter):
                self._resample_A(xxT, yxT, self.sigma)
                self._resample_sigma(xxT, yxT, yyT, n, self.A)

    def _resample_A(self, xxT, yxT, sigma):
        sigmainv = np.linalg.inv(sigma)
        J = self.J_0 + np.kron(sigmainv, xxT)
        h = self.h_0 + sigmainv.dot(yxT)
        self.A = sample_gaussian(J=J,h=h.ravel()).reshape(h.shape)

    def _resample_sigma(self, xxT, yxT, yyT, n, A):
        S = self.S_0 + yyT - yxT.dot(A.T) - A.dot(yxT.T) + A.dot(xxT).dot(A.T)
        nu = self.nu_0 + n
        self.sigma = sample_invwishart(S, nu)


class ARDRegression(Regression):
    def __init__(
            self, a,b,nu_0,S_0,M_0,
            blocksizes=None,K_0=None,niter=10,**kwargs):
        blocksizes = np.ones(M_0.shape[1],dtype=np.int64) \
            if blocksizes is None else blocksizes
        self.niter = niter
        self.blocksizes = np.array(blocksizes)
        self.starts = cumsum(blocksizes,strict=True)
        self.stops = cumsum(blocksizes,strict=False)

        self.a = np.repeat(a,len(blocksizes))
        self.b = np.repeat(b,len(blocksizes))

        self.nu_0 = nu_0
        self.S_0 = S_0
        self.M_0 = M_0

        if K_0 is None:
            self.resample_K()
        else:
            self.K_0 = K_0

        super(ARDRegression,self).__init__(
            K_0=self.K_0,nu_0=nu_0,S_0=S_0,M_0=M_0,**kwargs)

    def resample(self,data=[],stats=None):
        if len(data) > 0 or stats is not None:
            stats = self._get_statistics(data) if stats is None else stats
            for itr in xrange(self.niter):
                self.A, self.sigma = \
                    sample_mniw(*self._natural_to_standard(
                        self.natural_hypparam + stats))

                mat = self.M_0 - self.A
                self.resample_K(1./2*np.einsum(
                    'ij,ij->j',mat,np.linalg.solve(self.sigma,mat)))
        else:
            self.resample_K()
            super(ARDRegression,self).resample()

    def resample_K(self,diag=None):
        if diag is None:
            a, b = self.a, self.b
        else:
            sums = [diag[start:stop].sum()
                    for start,stop in zip(self.starts,self.stops)]
            a = self.a + self.D_out*self.blocksizes/2.
            b = self.b + np.array(sums)

        ks = 1./np.random.gamma(a,scale=1./b)
        self.K_0 = np.diag(np.repeat(ks,self.blocksizes))

        self.natural_hypparam = self._standard_to_natural(
            self.nu_0,self.S_0,self.M_0,self.K_0)

    @property
    def parameters(self):
        return (self.A, self.sigma, self.K_0)

    @parameters.setter
    def parameters(self, A_sigma_K_0_tuple1):
        (A,sigma,K_0) = A_sigma_K_0_tuple1
        self.A = A
        self.sigma = sigma
        self.K_0 = K_0
