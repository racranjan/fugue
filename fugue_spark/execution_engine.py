import logging
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

import pandas as pd
import pyarrow as pa
import pyspark.sql as ps
from fugue.collections.partition import (
    EMPTY_PARTITION_SPEC,
    PartitionCursor,
    PartitionSpec,
)
from fugue.constants import KEYWORD_ROWCOUNT
from fugue.dataframe import DataFrame, DataFrames, IterableDataFrame, LocalDataFrame
from fugue.dataframe.arrow_dataframe import ArrowDataFrame
from fugue.dataframe.pandas_dataframe import PandasDataFrame
from fugue.dataframe.utils import get_join_schemas
from fugue.execution.execution_engine import (
    _DEFAULT_JOIN_KEYS,
    ExecutionEngine,
    SQLEngine,
)
from fugue_spark._constants import FUGUE_SPARK_DEFAULT_CONF
from fugue_spark.dataframe import SparkDataFrame
from fugue_spark._utils.convert import to_schema, to_spark_schema, to_type_safe_input
from fugue_spark._utils.io import SparkIO
from fugue_spark._utils.partition import (
    even_repartition,
    hash_repartition,
    rand_repartition,
)
from pyspark import StorageLevel
from pyspark.rdd import RDD
from pyspark.sql import SparkSession
from pyspark.sql.functions import PandasUDFType, broadcast, col, pandas_udf
from triad.collections import ParamDict, Schema
from triad.collections.fs import FileSystem
from triad.utils.assertion import assert_arg_not_none, assert_or_throw
from triad.utils.hash import to_uuid
from triad.utils.iter import EmptyAwareIterable
from triad.utils.threading import RunOnce

_TO_SPARK_JOIN_MAP: Dict[str, str] = {
    "inner": "inner",
    "leftouter": "left_outer",
    "rightouter": "right_outer",
    "fullouter": "outer",
    "cross": "cross",
    "semi": "left_semi",
    "anti": "left_anti",
    "leftsemi": "left_semi",
    "leftanti": "left_anti",
}


class SparkSQLEngine(SQLEngine):
    """`Spark SQL <https://spark.apache.org/sql/>`_ execution implementation.

    :param execution_engine: it must be :class:`~.SparkExecutionEngine`
    :raises ValueError: if the engine is not :class:`~.SparkExecutionEngine`
    """

    def __init__(self, execution_engine: ExecutionEngine) -> None:
        assert_or_throw(
            isinstance(execution_engine, SparkExecutionEngine),
            ValueError("SparkSQLEngine must use SparkExecutionEngine"),
        )
        return super().__init__(execution_engine)

    def select(self, dfs: DataFrames, statement: str) -> DataFrame:
        for k, v in dfs.items():
            self.execution_engine.register(v, k)  # type: ignore
        return SparkDataFrame(
            self.execution_engine.spark_session.sql(statement)  # type: ignore
        )


class SparkExecutionEngine(ExecutionEngine):
    """The execution engine based on :class:`~spark:pyspark.sql.SparkSession`.

    Please read |ExecutionEngineTutorial| to understand this important Fugue concept

    :param spark_session: Spark session, defaults to None to get the Spark session by
      :meth:`~spark:pyspark.sql.SparkSession.Builder.getOrCreate`
    :param conf: |ParamsLikeObject| defaults to None, read |FugueConfig| to
      learn Fugue specific options
    """

    def __init__(self, spark_session: Optional[SparkSession] = None, conf: Any = None):
        if spark_session is None:
            spark_session = SparkSession.builder.getOrCreate()
        self._spark_session = spark_session
        cf = dict(FUGUE_SPARK_DEFAULT_CONF)
        cf.update({x[0]: x[1] for x in spark_session.sparkContext.getConf().getAll()})
        cf.update(ParamDict(conf))
        super().__init__(cf)
        self._fs = FileSystem()
        self._log = logging.getLogger()
        self._default_sql_engine = SparkSQLEngine(self)
        self._broadcast_func = RunOnce(
            self._broadcast, lambda *args, **kwargs: id(args[0])
        )
        self._persist_func = RunOnce(self._persist, lambda *args, **kwargs: id(args[0]))
        self._register_func = RunOnce(
            self._register, lambda *args, **kwargs: id(args[0])
        )
        self._io = SparkIO(self.spark_session, self.fs)

    def __repr__(self) -> str:
        return "SparkExecutionEngine"

    @property
    def spark_session(self) -> SparkSession:
        """
        :return: The wrapped spark session
        :rtype: :class:`spark:pyspark.sql.SparkSession`
        """
        return self._spark_session

    @property
    def log(self) -> logging.Logger:
        return self._log

    @property
    def fs(self) -> FileSystem:
        return self._fs

    @property
    def default_sql_engine(self) -> SQLEngine:
        return self._default_sql_engine

    def stop(self) -> None:
        """For now, it does NOT stop the underlying spark session"""
        # TODO: we need a conf to control whether to stop session
        # self.spark_session.stop()
        pass

    def to_df(
        self, df: Any, schema: Any = None, metadata: Any = None
    ) -> SparkDataFrame:
        """Convert a data structure to :class:`~fugue_spark.dataframe.SparkDataFrame`

        :param data: :class:`~fugue.dataframe.dataframe.DataFrame`,
          :class:`spark:pyspark.sql.DataFrame`, :class:`spark:pyspark.RDD`,
          pandas DataFrame or
          list or iterable of arrays
        :param schema: |SchemaLikeObject| or :class:`spark:pyspark.sql.types.StructType`
          defaults to None.
        :param metadata: |ParamsLikeObject|, defaults to None
        :return: engine compatible dataframe

        :Notice:

        * if the input is already :class:`~fugue_spark.dataframe.SparkDataFrame`,
          it should return itself
        * For :class:`~spark:pyspark.RDD`, list or iterable of arrays,
          ``schema`` must be specified
        * When ``schema`` is not None, a potential type cast may happen to ensure
          the dataframe's schema.
        * all other methods in the engine can take arbitrary dataframes and
          call this method to convert before doing anything
        """
        if isinstance(df, DataFrame):
            assert_or_throw(
                schema is None and metadata is None,
                ValueError("schema and metadata must be None when df is a DataFrame"),
            )
            if isinstance(df, SparkDataFrame):
                return df
            if any(pa.types.is_struct(t) for t in df.schema.types):
                sdf = self.spark_session.createDataFrame(
                    df.as_array(type_safe=True), to_spark_schema(df.schema)
                )
            else:
                sdf = self.spark_session.createDataFrame(
                    df.as_pandas(), to_spark_schema(df.schema)
                )
            return SparkDataFrame(sdf, df.schema, df.metadata)
        if isinstance(df, ps.DataFrame):
            return SparkDataFrame(
                df, None if schema is None else to_schema(schema), metadata
            )
        if isinstance(df, RDD):
            assert_arg_not_none(schema, "schema")
            sdf = self.spark_session.createDataFrame(df, to_spark_schema(schema))
            return SparkDataFrame(sdf, to_schema(schema), metadata)

        # use arrow dataframe here to handle nulls in int cols
        adf = ArrowDataFrame(df, to_schema(schema))
        sdf = self.spark_session.createDataFrame(
            adf.as_array(), to_spark_schema(adf.schema)
        )
        return SparkDataFrame(sdf, adf.schema, metadata)

    def repartition(self, df: DataFrame, partition_spec: PartitionSpec) -> DataFrame:
        def _persist_and_count(df: DataFrame) -> int:
            df = self.persist(df)
            return df.count()

        df = self.to_df(df)
        num_funcs = {KEYWORD_ROWCOUNT: lambda: _persist_and_count(df)}
        num = partition_spec.get_num_partitions(**num_funcs)

        if partition_spec.algo == "hash":
            sdf = hash_repartition(
                self.spark_session, df.native, num, partition_spec.partition_by
            )
        elif partition_spec.algo == "rand":
            sdf = rand_repartition(
                self.spark_session, df.native, num, partition_spec.partition_by
            )
        elif partition_spec.algo == "even":
            df = self.persist(df)
            sdf = even_repartition(
                self.spark_session, df.native, num, partition_spec.partition_by
            )
        else:  # pragma: no cover
            raise NotImplementedError(partition_spec.algo + " is not supported")
        sorts = partition_spec.get_sorts(df.schema)
        if len(sorts) > 0:
            sdf = sdf.sortWithinPartitions(
                *sorts.keys(), ascending=list(sorts.values())
            )
        return self.to_df(sdf, df.schema, df.metadata)

    def map(
        self,
        df: DataFrame,
        map_func: Callable[[PartitionCursor, LocalDataFrame], LocalDataFrame],
        output_schema: Any,
        partition_spec: PartitionSpec,
        metadata: Any = None,
        on_init: Optional[Callable[[int, DataFrame], Any]] = None,
    ) -> DataFrame:
        if (
            self.conf.get_or_throw("fugue.spark.use_pandas_udf", bool)
            and len(partition_spec.partition_by) > 0
            and not any(pa.types.is_nested(t) for t in Schema(output_schema).types)
        ):
            return self._map_by_pandas_udf(
                df,
                map_func=map_func,
                output_schema=output_schema,
                partition_spec=partition_spec,
                metadata=metadata,
                on_init=on_init,
            )
        df = self.to_df(self.repartition(df, partition_spec))
        mapper = _Mapper(df, map_func, output_schema, partition_spec, on_init)
        sdf = df.native.rdd.mapPartitionsWithIndex(mapper.run, True)
        return self.to_df(sdf, output_schema, metadata)

    def broadcast(self, df: DataFrame) -> SparkDataFrame:
        return self._broadcast_func(self.to_df(df))

    def persist(self, df: DataFrame, level: Any = None) -> SparkDataFrame:
        return self._persist_func(self.to_df(df), level)

    def register(self, df: DataFrame, name: str) -> SparkDataFrame:
        return self._register_func(self.to_df(df), name)

    def join(
        self,
        df1: DataFrame,
        df2: DataFrame,
        how: str,
        on: List[str] = _DEFAULT_JOIN_KEYS,
        metadata: Any = None,
    ) -> DataFrame:
        key_schema, output_schema = get_join_schemas(df1, df2, how=how, on=on)
        how = how.lower().replace("_", "").replace(" ", "")
        assert_or_throw(
            how in _TO_SPARK_JOIN_MAP,
            ValueError(f"{how} is not supported as a join type"),
        )
        how = _TO_SPARK_JOIN_MAP[how]
        d1 = self.to_df(df1).native
        d2 = self.to_df(df2).native
        cols = [col(n) for n in output_schema.names]
        if how == "cross":
            res = d1.crossJoin(d2).select(*cols)
        else:
            res = d1.join(d2, on=key_schema.names, how=how).select(*cols)
        return self.to_df(res, output_schema, metadata)

    def load_df(
        self,
        path: Union[str, List[str]],
        format_hint: Any = None,
        columns: Any = None,
        **kwargs: Any,
    ) -> DataFrame:
        return self._io.load_df(
            uri=path, format_hint=format_hint, columns=columns, **kwargs
        )

    def save_df(
        self,
        df: DataFrame,
        path: str,
        format_hint: Any = None,
        mode: str = "overwrite",
        partition_spec: PartitionSpec = EMPTY_PARTITION_SPEC,
        force_single: bool = False,
        **kwargs: Any,
    ) -> None:
        df = self.to_df(df)
        self._io.save_df(
            df,
            uri=path,
            format_hint=format_hint,
            mode=mode,
            partition_spec=partition_spec,
            force_single=force_single,
            **kwargs,
        )

    def _broadcast(self, df: SparkDataFrame) -> SparkDataFrame:
        sdf = broadcast(df.native)
        return SparkDataFrame(sdf, df.schema, df.metadata)

    def _persist(self, df: SparkDataFrame, level: Any) -> SparkDataFrame:
        if level is None:
            level = StorageLevel.MEMORY_AND_DISK
        if isinstance(level, str) and level in StorageLevel.__dict__:
            level = StorageLevel.__dict__[level]
        if isinstance(level, StorageLevel):
            df.native.persist()
            self.log.info(f"Persist dataframe with {level}, count {df.count()}")
            return df
        raise ValueError(f"{level} is not supported persist type")  # pragma: no cover

    def _register(self, df: SparkDataFrame, name: str) -> SparkDataFrame:
        df.native.createOrReplaceTempView(name)
        return df

    def _map_by_pandas_udf(
        self,
        df: DataFrame,
        map_func: Callable[[PartitionCursor, LocalDataFrame], LocalDataFrame],
        output_schema: Any,
        partition_spec: PartitionSpec,
        metadata: Any = None,
        on_init: Optional[Callable[[int, DataFrame], Any]] = None,
    ) -> DataFrame:
        presort = partition_spec.presort
        presort_keys = list(presort.keys())
        presort_asc = list(presort.values())
        output_schema = Schema(output_schema)
        input_schema = df.schema
        on_init_once: Any = (
            None
            if on_init is None
            else RunOnce(
                on_init, lambda *args, **kwargs: to_uuid(id(on_init), id(args[0]))
            )
        )

        def _udf(pdf: Any) -> pd.DataFrame:  # pragma: no cover
            if pdf.shape[0] == 0:
                return PandasDataFrame([], output_schema).as_pandas()
            if len(presort_keys) > 0:
                pdf = pdf.sort_values(presort_keys, ascending=presort_asc)
            input_df = PandasDataFrame(
                pdf.reset_index(drop=True), input_schema, pandas_df_wrapper=True
            )
            if on_init_once is not None:
                on_init_once(0, input_df)
            cursor = partition_spec.get_cursor(input_schema, 0)
            cursor.set(input_df.peek_array(), 0, 0)
            output_df = map_func(cursor, input_df)
            return output_df.as_pandas()

        df = self.to_df(df)
        udf = pandas_udf(
            _udf, to_spark_schema(output_schema), PandasUDFType.GROUPED_MAP
        )
        sdf = df.native.groupBy(*partition_spec.partition_by).apply(udf)
        return SparkDataFrame(sdf, metadata=metadata)


class _Mapper(object):  # pragma: no cover
    # pytest can't identify the coverage, but this part is fully tested
    def __init__(
        self,
        df: DataFrame,
        map_func: Callable[[PartitionCursor, LocalDataFrame], LocalDataFrame],
        output_schema: Any,
        partition_spec: PartitionSpec,
        on_init: Optional[Callable[[int, DataFrame], Any]],
    ):
        super().__init__()
        self.schema = df.schema
        self.output_schema = Schema(output_schema)
        self.metadata = df.metadata
        self.partition_spec = partition_spec
        self.map_func = map_func
        self.on_init = on_init

    def run(self, no: int, rows: Iterable[ps.Row]) -> Iterable[Any]:
        df = IterableDataFrame(
            to_type_safe_input(rows, self.schema), self.schema, self.metadata
        )
        if df.empty:  # pragma: no cover
            return
        cursor = self.partition_spec.get_cursor(self.schema, no)
        if self.on_init is not None:
            self.on_init(no, df)
        if self.partition_spec.empty:
            partitions: Iterable[Tuple[int, int, EmptyAwareIterable]] = [
                (0, 0, df.native)
            ]
        else:
            partitioner = self.partition_spec.get_partitioner(self.schema)
            partitions = partitioner.partition(df.native)
        for pn, sn, sub in partitions:
            cursor.set(sub.peek(), pn, sn)
            sub_df = IterableDataFrame(sub, self.schema)
            sub_df._metadata = self.metadata
            res = self.map_func(cursor, sub_df)
            for r in res.as_array_iterable(type_safe=True):
                yield r
