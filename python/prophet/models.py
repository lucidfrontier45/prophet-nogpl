# -*- coding: utf-8 -*-
# Copyright (c) Facebook, Inc. and its affiliates.

# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import absolute_import, division, print_function
from abc import abstractmethod, ABC
from typing import Tuple
from collections import OrderedDict
from enum import Enum
from pathlib import Path
import os
import pickle
import pkg_resources
import platform

import logging
logger = logging.getLogger('prophet.models')

PLATFORM = "unix"
if platform.platform().startswith("Win"):
    PLATFORM = "win"

class IStanBackend(ABC):
    def __init__(self):
        self.model = self.load_model()
        self.stan_fit = None
        self.newton_fallback = True

    def set_options(self, **kwargs):
        """
        Specify model options as kwargs.
         * newton_fallback [bool]: whether to fallback to Newton if L-BFGS fails
        """
        for k, v in kwargs.items():
            if k == 'newton_fallback':
                self.newton_fallback = v
            else:
                raise ValueError(f'Unknown option {k}')


    @staticmethod
    @abstractmethod
    def get_type():
        pass

    @abstractmethod
    def load_model(self):
        pass

    @abstractmethod
    def fit(self, stan_init, stan_data, **kwargs) -> dict:
        pass

    @abstractmethod
    def sampling(self, stan_init, stan_data, samples, **kwargs) -> dict:
        pass


_model_dir_path = Path(os.environ.get("PROPHET_MODEL_DIR_PATH", default=Path.home().joinpath(".prophet")))


class CmdStanPyBackend(IStanBackend):
    CMDSTAN_VERSION = "2.26.1"
    def __init__(self):
        super().__init__()
        # import cmdstanpy
        # cmdstanpy.set_cmdstan_path(
        #     pkg_resources.resource_filename("prophet", f"stan_model/cmdstan-{self.CMDSTAN_VERSION}")
        # )

    def download_model_files(self):
        if (platform.system(), platform.machine()) != ("Linux", "x86_64"):
            logger.warning("only Linux x86_64 binary can be downloaded")
            logger.warning("please prepare compiled stan model binary by yourself")
            return

        import requests
        import shutil

        if not _model_dir_path.exists():
            _model_dir_path.mkdir()

        targets = {
            "libtbb.so.2": "https://github.com/lucidfrontier45/prophet-nogpl/releases/download/1.0.0/libtbb.so.2",
            "prophet_model.bin": "https://github.com/lucidfrontier45/prophet-nogpl/releases/download/1.0.0/prophet_model.bin"
        }

        for file_name, url in targets.items():
            target_path = _model_dir_path.joinpath(file_name)
            if not target_path.exists():
                logger.info(f"downloading {file_name}")
                with requests.get(url, stream=True) as r:
                    with target_path.open("wb") as f:
                        shutil.copyfileobj(r.raw, f)
                        target_path.chmod(0o755)

    @staticmethod
    def get_type():
        return StanBackendEnum.CMDSTANPY.name

    def _add_tbb_to_path(self):
        """Add the TBB library to $PATH on Windows only. Required for loading model binaries."""
        if PLATFORM == "win":
            tbb_path = pkg_resources.resource_filename(
                "prophet",
                f"stan_model/cmdstan-{self.CMDSTAN_VERSION}/stan/lib/stan_math/lib/tbb"
            )
            os.environ["PATH"] = ";".join(
                list(OrderedDict.fromkeys([tbb_path] + os.environ.get("PATH", "").split(";")))
            )
        elif PLATFORM == "unix":
            tbb_path = str(_model_dir_path)
            old = os.environ.get("LD_LIBRARY_PATH")
            if old:
                os.environ["LD_LIBRARY_PATH"] = old + ":" + tbb_path
            else:
                os.environ["LD_LIBRARY_PATH"] = tbb_path

    def load_model(self):
        self.download_model_files()
        import cmdstanpy
        self._add_tbb_to_path()
        model_file = str(_model_dir_path.joinpath("prophet_model.bin"))
        return cmdstanpy.CmdStanModel(exe_file=model_file)

    def fit(self, stan_init, stan_data, **kwargs):
        (stan_init, stan_data) = self.prepare_data(stan_init, stan_data)

        if 'inits' not in kwargs and 'init' in kwargs:
            kwargs['inits'] = self.prepare_data(kwargs['init'], stan_data)[0]

        args = dict(
            data=stan_data,
            inits=stan_init,
            algorithm='Newton' if stan_data['T'] < 100 else 'LBFGS',
            iter=int(1e4),
        )
        args.update(kwargs)

        try:
            self.stan_fit = self.model.optimize(**args)
        except RuntimeError as e:
            # Fall back on Newton
            if self.newton_fallback and args['algorithm'] != 'Newton':
                logger.warning(
                    'Optimization terminated abnormally. Falling back to Newton.'
                )
                args['algorithm'] = 'Newton'
                self.stan_fit = self.model.optimize(**args)
            else:
                raise e

        params = self.stan_to_dict_numpy(
            self.stan_fit.column_names, self.stan_fit.optimized_params_np)
        for par in params:
            params[par] = params[par].reshape((1, -1))
        return params

    def sampling(self, stan_init, stan_data, samples, **kwargs) -> dict:
        (stan_init, stan_data) = self.prepare_data(stan_init, stan_data)

        if 'inits' not in kwargs and 'init' in kwargs:
            kwargs['inits'] = self.prepare_data(kwargs['init'], stan_data)[0]

        args = dict(
            data=stan_data,
            inits=stan_init,
        )

        if 'chains' not in kwargs:
            kwargs['chains'] = 4
        iter_half = samples // 2
        kwargs['iter_sampling'] = iter_half
        if 'iter_warmup' not in kwargs:
            kwargs['iter_warmup'] = iter_half

        args.update(kwargs)

        self.stan_fit = self.model.sample(**args)
        res = self.stan_fit.draws()
        (samples, c, columns) = res.shape
        res = res.reshape((samples * c, columns))
        params = self.stan_to_dict_numpy(self.stan_fit.column_names, res)

        for par in params:
            s = params[par].shape
            if s[1] == 1:
                params[par] = params[par].reshape((s[0],))

            if par in ['delta', 'beta'] and len(s) < 2:
                params[par] = params[par].reshape((-1, 1))

        return params

    @staticmethod
    def prepare_data(init, data) -> Tuple[dict, dict]:
        cmdstanpy_data = {
            'T': data['T'],
            'S': data['S'],
            'K': data['K'],
            'tau': data['tau'],
            'trend_indicator': data['trend_indicator'],
            'y': data['y'].tolist(),
            't': data['t'].tolist(),
            'cap': data['cap'].tolist(),
            't_change': data['t_change'].tolist(),
            's_a': data['s_a'].tolist(),
            's_m': data['s_m'].tolist(),
            'X': data['X'].to_numpy().tolist(),
            'sigmas': data['sigmas']
        }

        cmdstanpy_init = {
            'k': init['k'],
            'm': init['m'],
            'delta': init['delta'].tolist(),
            'beta': init['beta'].tolist(),
            'sigma_obs': init['sigma_obs']
        }
        return (cmdstanpy_init, cmdstanpy_data)

    @staticmethod
    def stan_to_dict_numpy(column_names: Tuple[str, ...], data: 'np.array'):
        import numpy as np

        output = OrderedDict()

        prev = None

        start = 0
        end = 0
        two_dims = len(data.shape) > 1
        for cname in column_names:
            if "." in cname:
                parsed = cname.split(".")
            else:
                parsed = cname.split("[")

            curr = parsed[0]
            if prev is None:
                prev = curr

            if curr != prev:
                if prev in output:
                    raise RuntimeError(
                        "Found repeated column name"
                    )
                if two_dims:
                    output[prev] = np.array(data[:, start:end])
                else:
                    output[prev] = np.array(data[start:end])
                prev = curr
                start = end
                end += 1
            else:
                end += 1

        if prev in output:
            raise RuntimeError(
                "Found repeated column name"
            )
        if two_dims:
            output[prev] = np.array(data[:, start:end])
        else:
            output[prev] = np.array(data[start:end])
        return output


class PyStanBackend(IStanBackend):

    @staticmethod
    def get_type():
        return StanBackendEnum.PYSTAN.name

    def sampling(self, stan_init, stan_data, samples, **kwargs) -> dict:

        args = dict(
            data=stan_data,
            init=lambda: stan_init,
            iter=samples,
        )
        args.update(kwargs)
        self.stan_fit = self.model.sampling(**args)
        out = {}
        for par in self.stan_fit.model_pars:
            out[par] = self.stan_fit[par]
            # Shape vector parameters
            if par in ['delta', 'beta'] and len(out[par].shape) < 2:
                out[par] = out[par].reshape((-1, 1))
        return out

    def fit(self, stan_init, stan_data, **kwargs) -> dict:

        args = dict(
            data=stan_data,
            init=lambda: stan_init,
            algorithm='Newton' if stan_data['T'] < 100 else 'LBFGS',
            iter=1e4,
        )
        args.update(kwargs)
        try:
            self.stan_fit = self.model.optimizing(**args)
        except RuntimeError as e:
            # Fall back on Newton
            if self.newton_fallback and args['algorithm'] != 'Newton':
                logger.warning(
                    'Optimization terminated abnormally. Falling back to Newton.'
                )
                args['algorithm'] = 'Newton'
                self.stan_fit = self.model.optimizing(**args)
            else:
                raise e

        params = {}

        for par in self.stan_fit.keys():
            params[par] = self.stan_fit[par].reshape((1, -1))

        return params

    def load_model(self):
        """Load compiled Stan model"""
        model_file = pkg_resources.resource_filename(
            'prophet',
            'stan_model/prophet_model.pkl',
        )
        with Path(model_file).open('rb') as f:
            return pickle.load(f)


class StanBackendEnum(Enum):
    PYSTAN = PyStanBackend
    CMDSTANPY = CmdStanPyBackend

    @staticmethod
    def get_backend_class(name: str) -> IStanBackend:
        try:
            return StanBackendEnum[name].value
        except KeyError as e:
            raise ValueError("Unknown stan backend: {}".format(name)) from e
