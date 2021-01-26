"""
This file is part of CLIMADA.

Copyright (C) 2017 ETH Zurich, CLIMADA contributors listed in AUTHORS.

CLIMADA is free software: you can redistribute it and/or modify it under the
terms of the GNU Lesser General Public License as published by the Free
Software Foundation, version 3.

CLIMADA is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more details.

You should have received a copy of the GNU Lesser General Public License along
with CLIMADA. If not, see <https://www.gnu.org/licenses/>.

---

Define Uncertainty class.
"""

import pandas as pd
import numpy as np
import logging
import matplotlib.pyplot as plt

from SALib.sample import saltelli
from SALib.analyze import sobol

import SALib.sample as sas
import SALib.analyze as saa

from climada.engine import Impact
from climada.entity import ImpactFuncSet
from climada.entity import Exposures
from climada.hazard import Hazard
from climada.util.value_representation import value_to_monetary_unit as vtm

LOGGER = logging.getLogger(__name__)


class UncVar():
    """
    Uncertainty variable

    An uncertainty variable requires a single or multi-parameter function.
    The parameters must follow a given distribution.

    Examples
    --------

    Categorical variable function: LitPop exposures with m,n exponents in [0,5]
        def unc_var_cat(m, n):
            exp = Litpop()
            exp.set_country('CHE', exponent=[m, n])
            return exp
        distr_dict = {
            m: sp.stats.randint(low=0, high=5),
            n: sp.stats.randint(low=0, high=5)
            }

    Continuous variable function: Impact function for TC
        def imp_fun_tc(G, v_half, vmin, k, _id=1):
            imp_fun = ImpactFunc()
            imp_fun.haz_type = 'TC'
            imp_fun.id = _id
            imp_fun.intensity_unit = 'm/s'
            imp_fun.intensity = np.linspace(0, 150, num=100)
            imp_fun.mdd = np.repeat(1, len(imp_fun.intensity))
            imp_fun.paa = np.array([sigmoid_function(v, G, v_half, vmin, k)
                                    for v in imp_fun.intensity])
            imp_fun.check()
            impf_set = ImpactFuncSet()
            impf_set.append(imp_fun)
            return impf_set
        distr_dict = {"G": sp.stats.uniform(0.8, 1),
              "v_half": sp.stats.uniform(50, 100),
              "vmin": sp.stats.norm(loc=15, scale=30),
              "k": sp.stats.randint(low=1, high=9)
              }

    """

    def __init__(self, unc_var, distr_dict):
        """
        Initialize UncVar

        Parameters
        ----------
        unc_var : function
            Variable defined as a function of the uncertainty parameters
        distr_dict : dict
            Dictionary of the probability density distributions of the
            uncertainty parameters, with keys the matching the keyword
            arguments (i.e. uncertainty parameters) of the unc_var function.
            The distribution must be of type scipy.stats
            https://docs.scipy.org/doc/scipy/reference/stats.html

        Returns
        -------
        None.

        """
        self.labels = list(distr_dict.keys())
        self.distr_dict = distr_dict
        self.unc_var = unc_var

    def plot_distr(self):
        """
        Plot the distributions of the parameters of the uncertainty variable.

        Returns
        -------
        fig, ax: matplotlib.pyplot.fig, matplotlib.pyplot.ax
            The figure and axis handle of the plot.

        """
        nplots = len(self.distr_dict)
        nrows, ncols = int(nplots / 3) + 1, min(nplots, 3)
        fig, axis = plt.subplots(nrows=nrows, ncols=ncols, figsize=(20, 16))
        for ax, (param_name, distr) in zip(axis.flatten(), self.distr_dict.items()):
            x = np.linspace(distr.ppf(0.001), distr.ppf(0.999), 100)
            ax.plot(x, distr.pdf(x), label=param_name)
            ax.legend()
        return fig, axis

    def eval_unc_var(self, kwargs):
        """
        Evaluate the uncertainty variable.

        Parameters
        ----------
        kwargs :
            These parameters will be passed to self.unc_var.
            They must be the input parameters of the uncertainty variable .

        Returns
        -------

            Evaluated uncertainty variable

        """
        return self.unc_var(**kwargs)


class Uncertainty():
    """
    Uncertainty analysis class

    This is the base class to perform uncertainty analysis on the outputs of a
    climada.engine.impact.Impact() or climada.engine.costbenefit.CostBenefit()
    object.

    """

    def __init__(self, unc_vars=None, pool=None):
        """Initialize Unc

        Parameters
        ----------
        exp_unc : climada.engine.uncertainty.UncVar or climada.entity.Exposure
            Exposure uncertainty variable or Exposure
        impf_unc : climada.engine.uncertainty.UncVar or climada.entity.ImpactFuncSet
            Impactfunction uncertainty variable or Impact function
        haz_unc : climada.engine.uncertainty.UncVar or climada.hazard.Hazard
            Hazard uncertainty variable or Hazard
        pool : pathos.pools.ProcessPool
            Pool of CPUs for parralel computations. Default is None.

        Returns
        -------
        None.

        """
        
        if unc_vars:
            self.unc_vars = {}

        if pool:
            self.pool = pool
            LOGGER.info('Using %s CPUs.', self.pool.ncpus)
        else:
            self.pool = None

        self.params = pd.DataFrame()
        self.problem = {}


    @property
    def n_runs(self):
        """
        The effective number of runs needed for the sample size self.n_samples.

        Returns
        -------
        int
            effective number of runs

        """

        if isinstance(self.params, pd.DataFrame):
            return self.params.shape[0]
        else:
            return 0
        
    @property
    def param_labels(self):
        """
        Labels of all uncertainty
        parameters.

        Returns
        -------
        list of strings
            Labels of all uncertainty parameters.

        """
        return list(self.distr_dict.keys())


    @property
    def distr_dict(self):
        """
        Dictionary of all (exposure, imapct function, hazard) distributions.

        Returns
        -------
        distr_dict : dict( sp.stats objects )
            Dictionary of all distributions.

        """

        distr_dict = dict()
        for unc_var in self.unc_vars.values():
            distr_dict.update(unc_var.distr_dict)
        return distr_dict
   
    
    def make_sample(self, N, sampling_method='saltelli', **kwargs):
        """
        Make a sample for all parameters with their respective
        distributions using the chosen method from SALib.
 
        Parameters
        ----------
        N : int
            Number of samples as defined in SALib.sample.saltelli.sample().
        calc_second_order : boolean
            if True, calculate second-order sensitivities.

        Returns
        -------
        None.

        """
        self.sampling_method = sampling_method
        self.n_samples = N
        uniform_base_sample = self._make_uniform_base_sample(**kwargs)
        df_params = pd.DataFrame(uniform_base_sample, columns=self.param_labels)
        for param in list(df_params):
            df_params[param] = df_params[param].apply(
                self.distr_dict[param].ppf
                )
        self.params = df_params
        
        
    def _make_uniform_base_sample(self, **kwargs):
        """
        Make a uniform distributed [0,1] sample for the defined model
        uncertainty parameters (self.param_labels) with the chosen
        method from saLib (kwargs are the keyword arguments passed to the
        saLib method)
        https://salib.readthedocs.io/en/latest/api.html#sobol-sensitivity-analysis

        Parameters
        ----------
        calc_second_order : boolean
            if True, calculate second-order sensitivities.

        Returns
        -------
        sobol_params : np.matrix
            Returns a NumPy matrix containing the sampled uncertainty parameters using
            Saltelli’s sampling scheme.

        """
        
        problem = {
            'num_vars' : len(self.param_labels),
            'names' : self.param_labels,
            'bounds' : [[0, 1]]*len(self.param_labels)
            }
        self.problem = problem
        salib_sampling_method = getattr(sas, self.sampling_method)
        sample_params = salib_sampling_method.sample(problem = problem,
                                                     N = self.n_samples,
                                                     **kwargs)
        return sample_params


    def est_comp_time(self):
        """
        Estimate the computation time

        Returns
        -------
        None.

        """
        raise NotImplementedError()
    
        
    def _calc_metric_sensitivity(self, df_metric, analysis_method, **kwargs):
        
        sensitivity_dict = {}
        for metric in df_metric:
            Y = df_metric[metric].to_numpy()
            sensitivity_index = analysis_method.analyze(self.problem, Y, **kwargs)
            sensitivity_dict.update({metric: sensitivity_index})
            
        return sensitivity_dict
    
    
    
class UncImpact(Uncertainty):
    
    def __init__(self, exp_unc, impf_unc, haz_unc, pool=None):
        """Initialize Unc

        Parameters
        ----------
        exp_unc : climada.engine.uncertainty.UncVar or climada.entity.Exposure
            Exposure uncertainty variable or Exposure
        impf_unc : climada.engine.uncertainty.UncVar or climada.entity.ImpactFuncSet
            Impactfunction uncertainty variable or Impact function
        haz_unc : climada.engine.uncertainty.UncVar or climada.hazard.Hazard
            Hazard uncertainty variable or Hazard
        pool : pathos.pools.ProcessPool
            Pool of CPUs for parralel computations. Default is None.

        Returns
        -------
        None.

        """

        if pool:
            self.pool = pool
            LOGGER.info('Using %s CPUs.', self.pool.ncpus)
        else:
            self.pool = None
            
        self.unc_vars = {}

        if isinstance(exp_unc, Exposures):
            self.unc_vars.update(
                {'exp': UncVar(unc_var=lambda: exp_unc, distr_dict={})}
                )
        else:
            self.unc_vars.update({'exp' : exp_unc})

        if isinstance(impf_unc, ImpactFuncSet):
            self.unc_vars.update(
                {'impf' : UncVar(unc_var=lambda: impf_unc, distr_dict={})}
                )
        else:
            self.unc_vars.update({'impf' : impf_unc})

        if isinstance(haz_unc, Hazard):
            self.unc_vars.update(
                {'haz' : UncVar(unc_var=lambda: haz_unc, distr_dict={})}
                )
        else:
            self.unc_vars.update({'haz' : haz_unc})

        self.params = pd.DataFrame()
        self.problem = {}
        
        self.aai = pd.DataFrame()
        self.freq_curve = pd.DataFrame()
        self.eai_exp = pd.DataFrame()
        self.at_event = pd.DataFrame()
    
    
    def calc_impact_distribution(self,
                             rp=None,
                             calc_eai_exp=False,
                             calc_at_event=False,
                             ):
        """
        Computes the impact for each of the parameters set defined in
        uncertainty.params.
    
        By default, the aggregated average annual impact
        (impact.aai_agg) and the excees impact at return periods (rp) is
        computed and stored in self.aai_freq. Optionally, the impact at
        each centroid location is computed (this may require a larger
        amount of memory if the number of centroids is large).
    
        Parameters
        ----------
        rp : list(int), optional
            Return period in years to be computed.
            The default is [5, 10, 20, 50, 100, 250].
        calc_eai_exp : boolean, optional
            Toggle computation of the impact at each centroid location.
            The default is False.
        calc_at_event : boolean, optional
            Toggle computation of the impact for each event.
            The default is False.
    
        Returns
        -------
        None.
    
        """

        if rp is None:
            rp=[5, 10, 20, 50, 100, 250]
    
        aai_agg_list = []
        freq_curve_list = []
        if calc_eai_exp:
            eai_exp_list = []
        if calc_at_event:
            at_event_list = []
    
        self.rp = rp
        self.calc_eai_exp = calc_eai_exp
        self.calc_at_event = calc_at_event
    
        #Compute impact distributions
        if self.pool:
            chunksize = min(self.n_runs // self.pool.ncpus, 100)
            impact_metrics = self.pool.map(self._map_impact_eval,
                                           self.params.iterrows(),
                                           chunsize = chunksize)
    
        else:
    
            impact_metrics = map(self._map_impact_eval, self.params.iterrows())
    
        [
         aai_agg_list, freq_curve_list,
         eai_exp_list, at_event_list
         ] = list(zip(*impact_metrics))
    
    
        # Assign computed impact distribution data to self
        df_aai_agg = pd.DataFrame(aai_agg_list, columns = ['aai_agg'])
        self.aai_agg = df_aai_agg
    
        df_freq_curve = pd.DataFrame(freq_curve_list,
                                   columns=['rp' + str(n) for n in rp])
        self.freq_curve = df_freq_curve
    
        if calc_eai_exp:
            df_eai_exp = pd.DataFrame(eai_exp_list)
            self.eai_exp = df_eai_exp
    
        if calc_at_event:
            df_at_event = pd.DataFrame(at_event_list)
            self.at_event = df_at_event


    def _map_impact_eval(self, param_sample):
        """
        Map to compute impact for all parameter samples in parrallel

        Parameters
        ----------
        param_sample : pd.DataFrame.iterrows()
            Generator of the parameter samples

        Returns
        -------
        list
            impact metrics list for all samples containing aai_agg, rp_curve,
            eai_exp (if self.calc_eai_exp=True), and at_event (if
            self.calc_at_event=True)

        """

        # [1] only the rows of the dataframe passed by pd.DataFrame.iterrows()
        exp_params = param_sample[1][self.unc_vars['exp'].labels].to_dict()
        haz_params = param_sample[1][self.unc_vars['haz'].labels].to_dict()
        impf_params = param_sample[1][self.unc_vars['impf'].labels].to_dict()

        exp = self.unc_vars['exp'].eval_unc_var(exp_params)
        haz = self.unc_vars['haz'].eval_unc_var(haz_params)
        impf = self.unc_vars['impf'].eval_unc_var(impf_params)

        imp = Impact()
        imp.calc(exposures=exp, impact_funcs=impf, hazard=haz)
        
        
        # Extract from impact the chosen metrics
        rp_curve = imp.calc_freq_curve(self.rp).impact
        
        if self.calc_eai_exp:
            eai_exp = imp.eai_exp
        else:
            eai_exp = None
            
        if self.calc_at_event:
            at_event= imp.at_event
        else:
            at_event = None

        return [imp.aai_agg, rp_curve, eai_exp, at_event]
    
                    
    def plot_impact_uncertainty(self):
        """
        Plot the distribution of values.

        Raises
        ------
        ValueError
            DESCRIPTION.

        Returns
        -------
        fig : TYPE
            DESCRIPTION.
        axes : TYPE
            DESCRIPTION.

        """
        if self.aai_agg.empty:
            raise ValueError("No uncertainty data present. Please run "+
                    "an uncertainty analysis first.")

        log_aai_freq = pd.concat([self.aai_agg.copy(),
                                  self.freq_curve.copy()],
                                 axis=1, join='inner') 
        
        log_aai_freq = log_aai_freq.apply(np.log10)
        log_aai_freq = log_aai_freq.replace([np.inf, -np.inf], np.nan)
        cols = log_aai_freq.columns
        nplots = len(cols)
        nrows, ncols = int(nplots / 3) + 1, min(nplots, 3)
        fig, axes = plt.subplots(nrows = nrows,
                                 ncols = ncols,
                                 figsize=(20, ncols * 3.5),
                                 sharex=True,
                                 sharey=True)

        for ax, col in zip(axes.flatten(), cols):
            data = log_aai_freq[col]
            data.hist(ax=ax,  bins=100, density=True, histtype='step')
            avg = self.aai_freq[col].mean()
            std = self.aai_freq[col].std()
            ax.plot([np.log10(avg), np.log10(avg)], [0, 1],
                    color='red', linestyle='dashed',
                    label="avg=%.2f%s" %vtm(avg))
            ax.plot([np.log10(avg) - np.log10(std) / 2,
                     np.log10(avg) + np.log10(std) / 2],
                    [0.3, 0.3], color='red',
                    label="std=%.2f%s" %vtm(std))
            ax.set_title(col)
            ax.set_xlabel('value [log10]')
            ax.set_ylabel('density of events')
            ax.legend()

        return fig, axes

    
    def calc_impact_sensitivity(self, method='sobol', **kwargs):
        """
        Compute the sensitivity indices using the SALib library:
        https://salib.readthedocs.io/en/latest/api.html#sobol-sensitivity-analysis
    
        Simple introduction to default Sobol sensitivity
        https://en.wikipedia.org/wiki/Variance-based_sensitivity_analysis
    
        Parameters
        ----------
        N : int
            Number of samples as defined in SALib.sample.saltelli.sample()
        rp : list(int), optional
            Return period in years for which sensitivity indices are computed.
            The default is [5, 10, 20, 50, 100, 250.
        calc_eai_exp : boolean, optional
            Toggle computation of the sensitivity for the impact at each
            centroid location. The default is False.
        calc_at_event : boolean, optional
            Toggle computation of the impact for each event.
            The default is False.
        calc_second_order : boolean, optional
            if True, calculate second-order sensitivities. The default is True.
        method : string, optional
            Choose the method for the sensitivity analysis. Note that
            saLib recommends pairs of sampling anad analysis algorithms.
            We recommend users to respect these pairings. 
            Defaul: 'sobol' 
            Note that for the default 'sobol', negative sensitivity
            indices indicate that the algorithm has not converged. In this
            case, please restart the uncertainty and sensitivity analysis
            with an increased number of samples.
        **kwargs :
            These parameters will be passed to SALib.analyze.sobol.analyze()
            The default is num_resamples=100, conf_level=0.95,
            print_to_console=False, parallel=False, n_processors=None,
            seed=None.
    
        Returns
        -------
        sobol_analysis : dict
            Dictionary with keys the uncertainty parameter labels.
            For each uncertainty parameter, the item is another dictionary
            with keys the sensitivity indices.    
        """
        
        if self.params.empty:
            raise ValueError("I found no samples. Please produce first"
                             " samples using Uncertainty.make_sample().")
            
        if self.aai_agg.empty:
            raise ValueError("I found no impact data. Please compute"
                             " the impact distribution first using"+
                             " Uncertainty.calc_impact_distribution()")
                              
        analysis_method = getattr(saa, method)
        sensitivity_analysis = {}
        
        aai_agg_sens = self._calc_metric_sensitivity(self.aai_agg, analysis_method, **kwargs)
        sensitivity_analysis.update(aai_agg_sens)
        
        freq_curve_sens = self._calc_metric_sensitivity(self.freq_curve, analysis_method, **kwargs)
        sensitivity_analysis.update(freq_curve_sens)
        
        if self.calc_eai_exp:
            eai_exp_sens = self._calc_metric_sensitivity(self.eai_exp, analysis_method, **kwargs)
            sensitivity_analysis.update(eai_exp_sens)
    
        if self.calc_at_event:
            at_event_sens = self._calc_metric_sensitivity(self.at_event, analysis_method, **kwargs)
            sensitivity_analysis.update(at_event_sens)
    
        self.sensitivity = sensitivity_analysis
    
        return sensitivity_analysis
    
    
class UncCostBenefit(Uncertainty):
    
    
    def calc_cost_benefit_distribution(self):
        raise NotImplementedError()
        
        
    def calc_cost_benefit_sensitivity(self,  method, **kwargs):
        raise NotImplementedError()


class UncRobustness():
    """
    Compute variance from multiplicative Gaussian noise
    """
    
    def __init__(self):
        raise NotImplementedError()
