#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

import functools
import itertools
import math
import random
from typing import Any, Dict, Iterable, MutableMapping, Optional, Type

from pytext.common.constants import Stage
from pytext.config.component import Component, ComponentType, Registry, create_component

from .sources import DataSource, RawExample, TSVDataSource
from .sources.data_source import (
    GeneratorIterator,
    RowShardedDataSource,
    ShardedDataSource,
)
from .tensorizers import MetricTensorizer, Tensorizer, initialize_tensorizers


class Batcher(Component):
    """Batcher designed to batch rows of data, before padding."""

    __COMPONENT_TYPE__ = ComponentType.BATCHER
    __EXPANSIBLE__ = True

    class Config(Component.Config):
        #: Make batches of this size when possible. If there's not enough data,
        #: might generate some smaller batches.
        train_batch_size: int = 16
        eval_batch_size: int = 16
        test_batch_size: int = 16

    @classmethod
    def from_config(cls, config: Config):
        return cls(
            config.train_batch_size, config.eval_batch_size, config.test_batch_size
        )

    def __init__(
        self,
        train_batch_size=Config.train_batch_size,
        eval_batch_size=Config.eval_batch_size,
        test_batch_size=Config.test_batch_size,
    ):
        self.train_batch_size = train_batch_size
        self.eval_batch_size = eval_batch_size
        self.test_batch_size = test_batch_size
        self._batch_sizes = {
            Stage.TRAIN: self.train_batch_size,
            Stage.TEST: self.test_batch_size,
            Stage.EVAL: self.eval_batch_size,
        }

    def batchify(
        self, iterable: Iterable[RawExample], sort_key=None, stage=Stage.TRAIN
    ):
        """Group rows by batch_size.  Assume iterable of dicts, yield dict of lists.
        The last batch will be of length len(iterable) % batch_size."""
        batch_size = self._batch_sizes[stage]
        for batch in self._group_iter(iterable, batch_size, sort_key):
            yield zip_dicts(batch)

    def _group_iter(self, iterable: Iterable[RawExample], group_size, sort_key=None):
        iterators = [iter(iterable)] * group_size
        for group in itertools.zip_longest(*iterators):
            group = [ex for ex in group if ex is not None]
            if sort_key:
                group.sort(key=sort_key, reverse=True)
            yield group


class PoolingBatcher(Batcher):
    """
    Batcher that looks at pools of data, and sorts, batches, and shuffles them, before
    padding.
    """

    class Config(Batcher.Config):
        #: Number of batches in a pool, to load at one time.
        pool_num_batches: int = 10000

    @classmethod
    def from_config(cls, config: Config):
        return cls(
            config.train_batch_size,
            config.eval_batch_size,
            config.test_batch_size,
            config.pool_num_batches,
        )

    def __init__(
        self,
        train_batch_size=Config.train_batch_size,
        eval_batch_size=Config.eval_batch_size,
        test_batch_size=Config.test_batch_size,
        pool_num_batches=Config.pool_num_batches,
    ):
        super().__init__(train_batch_size, eval_batch_size, test_batch_size)
        self.pool_num_batches = pool_num_batches or 1

    def batchify(
        self, iterable: Iterable[RawExample], sort_key=None, stage=Stage.TRAIN
    ):
        """
        From an iterable of dicts, yield dicts of lists, by

        1. Load pool of batch_size * pool_num_batches examples.
        2. Sort rows, if necessary.
        3. Form batches with batch_size examples each.
        4. Shuffle batches and yield all batches.
        """
        batch_size = self._batch_sizes[stage]
        pool_size = batch_size * self.pool_num_batches

        for pool in self._group_iter(iterable, pool_size, sort_key):
            batch_indices = list(range(math.ceil(len(pool) / batch_size)))
            if sort_key:
                random.shuffle(batch_indices)
            else:
                random.shuffle(pool)
            for batch_index in batch_indices:
                batch = pool[batch_size * batch_index : batch_size * (batch_index + 1)]
                yield zip_dicts(batch)


def pad_and_tensorize_batches(tensorizers, batches):
    for batch in batches:
        tensor_dict = {}
        for name, tensorizer in tensorizers.items():
            if isinstance(tensorizer, MetricTensorizer):
                tensor_dict[name] = tensorizer.tensorize(batch)
            else:
                tensor_dict[name] = tensorizer.tensorize(batch[name])

        yield tensor_dict


def zip_dicts(dicts):
    all_keys = set(itertools.chain.from_iterable(dicts))
    zipped = {key: [] for key in all_keys}
    for d in dicts:
        for key in all_keys:
            zipped[key].append(d.get(key))
    return zipped


def generator_iterator(fn):
    """Turn a generator into a GeneratorIterator-wrapped function.
    Effectively this allows iterating over a generator multiple times by recording
    the call arguments, and calling the generator with them anew each item __iter__
    is called on the returned object."""

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        return GeneratorIterator(fn, *args, **kwargs)

    return wrapped


class Data(Component):
    """Data is an abstraction that handles all of the following:

    - Initialize model metadata parameters
    - Create batches of tensors for model training or prediction

    It can accomplish these in any way it needs to. The base implementation
    utilizes `pytext.data.sources.DataSource`, and sends batches to
    `pytext.data.tensorizers.Tensorizer` to create tensors.

    The `tensorizers` dict passed to the initializer should be considered something like
    a signature for the model. Each batch should be a dictionary with the same keys
    as the `tensorizers` dict, and values should be tensors arranged in the way
    specified by that tensorizer. The tensorizers dict doubles as a simple baseline
    implementation of that same signature, but subclasses of Data can override the
    implementation using other methods. This value is how the model specifies what
    inputs it's looking for.
    """

    __COMPONENT_TYPE__ = ComponentType.DATA_HANDLER
    __EXPANSIBLE__ = True

    class Config(Component.Config):
        #: Specify where training/test/eval data come from. The default value
        #: will not provide any data.
        source: DataSource.Config = TSVDataSource.Config()
        #: How training examples are split into batches for the optimizer.
        batcher: Batcher.Config = PoolingBatcher.Config()
        sort_key: Optional[str] = None
        #: define epoch to be a fixed number of batches.
        #: If not set, use the entire dataset
        epoch_size: Optional[int] = None
        #: cache numberized result in memory, turn off when CPU memory bound.
        in_memory: Optional[bool] = True

    @classmethod
    def from_config(
        cls,
        config: Config,
        schema: Dict[str, Type],
        tensorizers: Dict[str, Tensorizer],
        rank=0,
        world_size=1,
        **kwargs,
    ):
        data_source_cls = Registry.get(ComponentType.DATA_SOURCE, type(config.source))
        if issubclass(data_source_cls, ShardedDataSource):
            # data source is already sharded, we don't need to wrap RowShardedDataSource
            data_source = create_component(
                ComponentType.DATA_SOURCE,
                config.source,
                schema,
                rank=rank,
                world_size=world_size,
            )
        else:
            unsharded_data_source = create_component(
                ComponentType.DATA_SOURCE, config.source, schema
            )
            data_source = RowShardedDataSource(
                data_source=unsharded_data_source, rank=rank, world_size=world_size
            )

        batcher = create_component(ComponentType.BATCHER, config.batcher)
        return cls(
            data_source,
            tensorizers,
            batcher=batcher,
            sort_key=config.sort_key,
            epoch_size=config.epoch_size,
            in_memory=config.in_memory,
            **kwargs,
        )

    def __init__(
        self,
        data_source: DataSource,
        tensorizers: Dict[str, Tensorizer],
        batcher: Batcher = None,
        sort_key: Optional[str] = None,
        epoch_size: Optional[int] = None,
        in_memory: Optional[bool] = False,
    ):
        """This function should also initialize the passed in tensorizers with
        metadata they need for model construction."""
        self.data_source = data_source
        self.tensorizers = tensorizers
        self.batcher = batcher or Batcher()
        self.sort_key = sort_key
        self.epoch_size = epoch_size
        self.in_memory = in_memory
        self.batch = {Stage.TRAIN: None, Stage.EVAL: None, Stage.TEST: None}
        self.numberized_cache: MutableMapping[str, Any] = {}
        full_train_data = (
            data_source.train_unsharded
            if isinstance(data_source, ShardedDataSource)
            else data_source.train
        )
        initialize_tensorizers(self.tensorizers, full_train_data)

    def _get_batches(self, stage, data_source):
        if not self.batch[stage]:
            rows = {
                Stage.TRAIN: data_source.train,
                Stage.TEST: data_source.test,
                Stage.EVAL: data_source.eval,
            }[stage]

            if self.in_memory:
                numberized_rows = self.numberized_cache.get(stage, None)
                if not numberized_rows:
                    numberized_rows = list(self.numberize_rows(rows))
                    self.numberized_cache[stage] = numberized_rows
                else:
                    print(f"Get numberized rows from cache in stage: {stage}")
            else:
                numberized_rows = self.numberize_rows(rows)

            batches = self.batcher.batchify(
                numberized_rows,
                sort_key=(
                    lambda row: self.tensorizers[self.sort_key].sort_key(
                        row[self.sort_key]
                    )
                )
                if self.sort_key
                else None,
            )
            self.batch[stage] = iter(
                pad_and_tensorize_batches(self.tensorizers, batches)
            )

        return self.batch[stage]

    def numberize_rows(self, rows):
        for row in rows:
            yield {
                name: tensorizer.numberize(row)
                for name, tensorizer in self.tensorizers.items()
            }

    @generator_iterator
    def batches(self, stage: Stage, data_source=None):
        """Create batches of tensors to pass to model train_batch.
        This function yields dictionaries that mirror the `tensorizers` dict passed to
        `__init__`, ie. the keys will be the same, and the tensors will be the shape
        expected from the respective tensorizers.

        `stage` is used to determine which data source is used to create batches.
        if data_source is provided, it is used instead of the configured data_sorce
        this is to allow setting a different data_source for testing a model
        """
        data_source = data_source or self.data_source
        self.num_batches = 0
        while True:
            for batch in self._get_batches(stage, data_source):
                if stage == Stage.TRAIN and self.num_batches == self.epoch_size:
                    self.num_batches = 0
                    return
                self.num_batches += 1
                yield batch
            self.batch[stage] = None
            if stage != Stage.TRAIN or not self.epoch_size:
                return
