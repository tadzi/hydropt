import numpy as np
#import jax.numpy as np
#import jax
import warnings
import pandas as pd
import itertools
from xarray import DataArray, Dataset
from .band_models import BandModel
import types
#import pkg_resources
#from .iops import IOP_model
from abc import ABC, abstractmethod
from sklearn.preprocessing import PolynomialFeatures

''' TO DO:
# - move model coefficients to XML/JSON file and include bounds                                     
'''


# PACE_POLYNOM_05 = pkg_resources.resource_filename('hydropt', 'data/PACE_polynom_05.csv')
# OLCI_POLYNOM_04 = pkg_resources.resource_filename('hydropt', 'data/OLCI_polynom_04.csv')

PACE_POLYNOM_05 = pd.read_csv('/Users/tadzio/Documents/code_repo/hydropt_4_sent3/data/processed/model_coefficients/PACE_polynom_05.csv', index_col=0)

def der_2d_polynomial(x, c, p):
    '''
    derivative of 2 variable polynomial
    
    x: points at wich to evaluate the derivative; a nx2 array were n is number of wavebands
    c: coefficients of the polynomial terms; a nxm matrix where n is the number wavebands and m the number of polynomial terms
    p: exponents of the n polynomial terms; a mx2 matrix
    '''
    if c.shape[0] is not x.shape[0]:
        warnings.warn('matrix dimensions of x and c do not match!')
    # get derivative terms of polynomial features
    d_x1 = lambda x, p: p[0]*x[0]**(float(p[0]-1))*x[1]**p[1]
    d_x2 = lambda x, p: p[1]*x[1]**(float(p[1]-1))*x[0]**p[0]
    # evaluate terms at [x]
    ft = np.array([[d_x1(x,p), d_x2(x,p)] for (x,p) in zip(itertools.cycle([x.T]), p)])
    # dot derivative matrix with polynomial coefficients and get diagonal
    dx = np.array([np.dot(c,ft[:,0,:]).diagonal(), np.dot(c,ft[:,1,:]).diagonal()])
    
    return dx

class Interpolator:
    '''
    see descriptors: 
    https://stackoverflow.com/questions/55511445/how-to-pass-self-to-a-method-like-object-in-python
    '''
    def __init__(self, dims, method='linear'):
        self.dims = dims
        self.method = method

    def interpolate(self, da, xnew):
        return da.interp(**{self.dims: xnew}, method=self.method)

    def __call__(self, _instance, xnew):
        instance = _instance.__class__()
        # copy attributes
        instance.__dict__.update(_instance.__dict__)
        # replace _parameters w. interpolated values
        setattr(instance, '_parameters', self.interpolate(instance._parameters, xnew))

        return instance

    def __get__(self, instance, owner):
        return types.MethodType(self, instance) if instance else self
        

class WavebandError(ValueError):
    pass

''' Abstract base classes

Definition of all abstract base classes

ForwardModel: model to calculate Rrs from IOPs
'''

class ReflectanceModel(ABC):
    ''' 
    rename ForwardModel -> ReflectanceModel
    '''
    @property
    @abstractmethod
    def _domain(self):
        pass
    
    @abstractmethod
    def interpolate(self, xnew):
        '''return: instance of class'''
            
    @abstractmethod
    def forward(self, x):
        pass

    @abstractmethod
    def gradient(self, x):
        pass
    
    def plot(self):
        pass

''' Context classes

ForwardModel: ...
'''

class ForwardModel:
    '''
    BioOpticalModel -> ForwardModel
    '''
    def __init__(self, iop_model, refl_model):
        self.iop_model = iop_model
        self.refl_model = refl_model
        self.__cache = True
        # method does nothing yet -> pass to interpolate()
        self._method = 'linear'

    @property
    def iop_model(self):
        return self.__iop_model

    @iop_model.setter
    def iop_model(self, m):
        self.__iop_model = m
        self.__cache = True

    @property
    def refl_model(self):      
        return self.__refl_model

    @refl_model.setter
    def refl_model(self, m):
        self.__refl_model = m
        self.__cache = True
    
    def forward(self, **x):
        if self.__cache:
            self.refl_model = self.refl_model.interpolate(self.iop_model.wavebands)
            self.__cache = False
            
        iops = self.iop_model.sum_iop(**x)
        self._validate_bounds(x)
        
        #for jax.jacfwd/jacrev uncomment line below
        #return self.refl_model.forward(iops)
        return pd.Series(self.refl_model.forward(iops), index=self.iop_model.wavebands)
    
    def jacobian(self, **x):
        # init empty jacobian matrix
        jac = np.empty([self.iop_model.wavebands.size, len(x)])
        # calculate total a & bb
        iops = self.iop_model.sum_iop(**x)
        # get gradient of bio-optical models
        grad_iops = self.iop_model.get_gradient(**x)
        # get gradient of reflectance model
        grad_refl_model = self.refl_model.gradient(iops)
        # use tensor product instead?
        for c, (i,j) in enumerate(zip(grad_refl_model.T, grad_iops.T)):
            #for jax.jacfwd/jacrev uncomment line below
            #jac = jax.ops.index_update(jac, jax.ops.index[c], np.dot(i, j))
            jac[c] = np.dot(i,j)
        
        return jac

    def _validate_bounds(self, x):
        pass


class PolynomialReflectance(ReflectanceModel):

    _parameters = DataArray(PACE_POLYNOM_05)
    _domain = None
    _powers = PolynomialFeatures(degree=5).fit([[1,1]]).powers_
    interpolate = Interpolator(dims='wavelength')

    def forward(self, x):
        c = self._parameters.values
        x = np.log(x)
        # get polynomial features
        #ft = PolynomialFeatures(degree=5).fit_transform(x.T)
        ft = self._polynomial_features(x.T)
        # calculate log(Rrs)
        log_rrs = np.dot(c, ft.T).diagonal()

        return np.exp(log_rrs)

    def gradient(self, x):
        # evaluate derivative of 2d polynomial at ln(x)
        d_p = der_2d_polynomial(np.log(x.T), self._parameters.values, self._powers)
        # dln(a)/da = 1/a
        d_a = 1/x[0]
        # dln(bb)/dbb = 1/bb
        d_bb = 1/x[1]
        # dRrs/dln(Rrs) = Rrs
        rrs = self.forward(x)
        # full gradient
        dx = rrs*d_p*np.array([d_a, d_bb])

        return dx

    def _polynomial_features(self, x):
        x = x.T
        f = np.array([(x[0]**i)*(x[1]**j) for (i,j) in self._powers]).T

        return f

class PolynomialForward(ForwardModel):
    def __init__(self, iop_model):
        super().__init__(iop_model, PolynomialReflectance())


def _residual(x, y, f, w):
    '''weighted residuals'''
    
    return (f(**x)-y)/(1/np.sqrt(w))


class ValidationDataset:

    def __init__(self, fwd_model):
        self.fwd_model = fwd_model

    def create(self, wt):
        ''' create validation set

        wt - str indicating watertype: c1, c2 etc..
        '''

        pass

def _to_dataset(func):
    '''
    make this a validation set decorator

    to do: take extra argument of observed concentration and calculate
    rrs -> add noise -> invert using .invert() -> create validation set

    let this function create the synthetic dataset and invert it -> return the validation set:
    observed vs. predicted
    '''
    def wrapper(*args, **kwargs):
        '''
        m - InversionModel instance
        x, y, w - see InversionModel
        '''
        stat_vars = ['chisqr', 'redchi', 'aic', 'bic']
        fwd_model = args[0]._fwd_model.forward 
        iop_model = args[0]._fwd_model.iop_model
        # do inversion
        out = func(*args, **kwargs)
        # get estimates
        x_hat = {i: float(j) for i,j in out.params.items()}
        rrs_hat = fwd_model(**x_hat)
        iop_hat = iop_model.get_iop(**x_hat)
        # organize data in dict
        data = {
            'rrs': (['wavelength'], rrs_hat),
            'iops': (['comp', 'iop', 'wavelength'], iop_hat),
            'conc': (['comp'], [i for i in x_hat.values()]),
            'weights': (['wavelength'], kwargs.get('w', np.repeat(1, len(rrs_hat))))}
        # add stats
        data.update({i: getattr(out, i) for i in stat_vars})   
        # iop_hat = {k: v for k,v in zip(x_hat.keys(), iop_model.get_iop(**x_hat))}
        # calculate standard-error
        try:
            data.update({'std_error': (['comp'], np.sqrt(getattr(out, 'covar').diagonal()))})
        except AttributeError:
            data.update({'std_error': (['comp'], np.repeat(np.nan, len(x_hat)))})
        # set coordinates
        coords = {
            'wavelength': iop_model.wavebands,
            'comp': [i for i in x_hat.keys()],
            'iop': ['absorption', 'backscatter']}
        
        return Dataset(data, coords=coords)
    
    return wrapper

 
class InversionModel:
    def __init__(self, fwd_model, minimizer, loss=_residual, band_model='rrs'):
        self._fwd_model = fwd_model
        self._minimizer = minimizer
        self._loss = loss
        self._band_model = BandModel(band_model)

    #@_to_dataset
    def invert(self, y, x, w=1, jac=False):
        ''' 
        x - initial guess
        y - rrs to invert
        w - weights to wavebands
        jac - use analytical expression of jacobian if available

        decorate invert() to parse output to dict/DataFrame
        specify decorater class during init or as property
        https://stackoverflow.com/questions/51883058/l1-norm-instead-of-l2-norm-for-cost-function-in-regression-model
        '''
        #key, x0 = zip(*[(k, float(v)) for (k, v) in x.items()])
        loss_fun = lambda x, y, f: self._loss(dict(x.valuesdict()), y, f, w)
        if jac:
            # parse lmfit.Parameters to dict for jacobian method argument
            jac_fun = lambda x, y, f: self._fwd_model.jacobian(**dict(x.valuesdict()))
        else:
            jac_fun = None
        # apply band-transformation on y and model
        args = self._band_model((y, self._fwd_model.forward))
        # to do: implement jac (scipy.optimize)/Dfun (lmfit)
        xhat = self._minimizer(loss_fun, x, args=args, Dfun=jac_fun)
        warnings.warn('''no band transformation is applied to jacobian -
         o.k. when band_model = 'rrs' ''')
        
        return xhat

    @property
    def iop_model(self):
        return self._fwd_model.iop_model.iop_model
