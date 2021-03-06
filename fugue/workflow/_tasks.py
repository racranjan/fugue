from abc import ABC, abstractmethod
from typing import Any, List, Optional, no_type_check

from adagio.instances import TaskContext
from adagio.specs import InputSpec, OutputSpec, TaskSpec
from fugue.collections.partition import PartitionSpec
from fugue.dataframe import DataFrame, DataFrames
from fugue.dataframe.array_dataframe import ArrayDataFrame
from fugue.exceptions import FugueWorkflowError
from fugue.execution import ExecutionEngine
from fugue.extensions.creator.convert import _to_creator
from fugue.extensions.outputter.convert import _to_outputter
from fugue.extensions.processor.convert import _to_processor
from fugue.workflow._workflow_context import FugueWorkflowContext
from triad.collections.dict import ParamDict
from triad.exceptions import InvalidOperationError
from triad.utils.assertion import assert_or_throw
from triad.utils.hash import to_uuid


class FugueTask(TaskSpec, ABC):
    def __init__(
        self,
        input_n: int = 0,
        output_n: int = 0,
        configs: Any = None,
        params: Any = None,
        deterministic: bool = True,
        lazy: bool = False,
        input_names: Optional[List[str]] = None,
    ):
        assert_or_throw(
            output_n <= 1,  # TODO: for now we don't support multi output
            NotImplementedError("Fugue doesn't support multiple output tasks"),
        )
        if input_names is None:
            inputs = [
                InputSpec("_" + str(i), DataFrame, nullable=False)
                for i in range(input_n)
            ]
        else:
            inputs = [
                InputSpec(input_names[i], DataFrame, nullable=False)
                for i in range(input_n)
            ]
        outputs = [
            OutputSpec("_" + str(i), DataFrame, nullable=False) for i in range(output_n)
        ]
        self._input_has_key = input_names is not None
        super().__init__(
            configs=configs,
            inputs=inputs,
            outputs=outputs,
            func=self.execute,
            metadata=params,
            deterministic=deterministic,
            lazy=lazy,
        )
        self._persist: Any = None
        self._broadcast = False
        self._checkpoint = False
        self._checkpoint_namespace: Optional[str] = None

    def __uuid__(self) -> str:
        return to_uuid(
            self.configs,
            self.inputs,
            self.outputs,
            # get_full_type_path(self.func),
            self.metadata,
            self.deterministic,
            self.lazy,
            self.node_spec,
            str(self._persist),
            self._broadcast,
            self._checkpoint,
            self._checkpoint_namespace,
        )

    @abstractmethod
    def execute(self, ctx: TaskContext) -> None:  # pragma: no cover
        raise NotImplementedError

    @property
    def single_output_expression(self) -> str:
        assert_or_throw(
            len(self.outputs) == 1,
            FugueWorkflowError(f"{self.name} does not have single output"),
        )
        return self.name + "." + self.outputs.get_key_by_index(0)

    def copy(self) -> "FugueTask":
        raise InvalidOperationError("can't copy")

    def __copy__(self) -> "FugueTask":
        raise InvalidOperationError("can't copy")

    def __deepcopy__(self, memo: Any) -> "FugueTask":
        raise InvalidOperationError("can't copy")

    def checkpoint(self, namespace: Any = None):
        # TODO: currently checkpoint is not taking effect
        self._checkpoint = True
        self._checkpoint_namespace = None if namespace is None else str(namespace)

    def persist(self, level: Any) -> "FugueTask":
        self._persist = "" if level is None else level
        return self

    def handle_persist(self, df: DataFrame, engine: ExecutionEngine) -> DataFrame:
        if self._persist is None:
            return df
        return engine.persist(df, None if self._persist == "" else self._persist)

    def broadcast(self) -> "FugueTask":
        self._broadcast = True
        return self

    def handle_broadcast(self, df: DataFrame, engine: ExecutionEngine) -> DataFrame:
        if not self._broadcast:
            return df
        return engine.broadcast(df)

    # def pre_partition(self, *args: Any, **kwargs: Any) -> "FugueTask":
    #    self._pre_partition = PartitionSpec(*args, **kwargs)
    #    return self

    def _get_workflow_context(self, ctx: TaskContext) -> FugueWorkflowContext:
        wfctx = ctx.workflow_context
        assert isinstance(wfctx, FugueWorkflowContext)
        return wfctx

    def _get_execution_engine(self, ctx: TaskContext) -> ExecutionEngine:
        return self._get_workflow_context(ctx).execution_engine

    def _set_result(self, ctx: TaskContext, df: DataFrame) -> None:
        self._get_workflow_context(ctx).set_result(id(self), df)


class Create(FugueTask):
    @no_type_check
    def __init__(
        self,
        creator: Any,
        schema: Any = None,
        params: Any = None,
        deterministic: bool = True,
        lazy: bool = True,
    ):
        self._creator = _to_creator(creator, schema)
        self._creator._params = ParamDict(params)
        super().__init__(
            params=params, input_n=0, output_n=1, deterministic=deterministic, lazy=lazy
        )

    @no_type_check
    def __uuid__(self) -> str:
        return to_uuid(super().__uuid__(), self._creator, self._creator._params)

    @no_type_check
    def execute(self, ctx: TaskContext) -> None:
        e = self._get_execution_engine(ctx)
        self._creator._execution_engine = e
        df = self._creator.create()
        df = self.handle_persist(df, e)
        df = self.handle_broadcast(df, e)
        self._set_result(ctx, df)
        ctx.outputs["_0"] = df


class Process(FugueTask):
    @no_type_check
    def __init__(
        self,
        input_n: int,
        processor: Any,
        schema: Any,
        params: Any,
        pre_partition: Any = None,
        deterministic: bool = True,
        lazy: bool = False,
        input_names: Optional[List[str]] = None,
    ):
        self._processor = _to_processor(processor, schema)
        self._processor._params = ParamDict(params)
        self._processor._partition_spec = PartitionSpec(pre_partition)
        super().__init__(
            params=params,
            input_n=input_n,
            output_n=1,
            deterministic=deterministic,
            lazy=lazy,
            input_names=input_names,
        )

    @no_type_check
    def __uuid__(self) -> str:
        return to_uuid(
            super().__uuid__(),
            self._processor,
            self._processor._params,
            self._processor._partition_spec,
        )

    @no_type_check
    def execute(self, ctx: TaskContext) -> None:
        e = self._get_execution_engine(ctx)
        self._processor._execution_engine = e
        if self._input_has_key:
            df = self._processor.process(DataFrames(ctx.inputs))
        else:
            df = self._processor.process(DataFrames(ctx.inputs.values()))
        df = self.handle_persist(df, e)
        df = self.handle_broadcast(df, e)
        self._set_result(ctx, df)
        ctx.outputs["_0"] = df


class Output(FugueTask):
    @no_type_check
    def __init__(
        self,
        input_n: int,
        outputter: Any,
        params: Any,
        pre_partition: Any = None,
        deterministic: bool = True,
        lazy: bool = False,
        input_names: Optional[List[str]] = None,
    ):
        assert_or_throw(input_n > 0, FugueWorkflowError("must have at least one input"))
        self._outputter = _to_outputter(outputter)
        self._outputter._params = ParamDict(params)
        self._outputter._partition_spec = PartitionSpec(pre_partition)
        super().__init__(
            params=params,
            input_n=input_n,
            output_n=1,
            deterministic=deterministic,
            lazy=lazy,
            input_names=input_names,
        )

    @no_type_check
    def __uuid__(self) -> str:
        return to_uuid(
            super().__uuid__(),
            self._outputter,
            self._outputter._params,
            self._outputter._partition_spec,
        )

    @no_type_check
    def execute(self, ctx: TaskContext) -> None:
        self._outputter._execution_engine = self._get_execution_engine(ctx)
        if self._input_has_key:
            self._outputter.process(DataFrames(ctx.inputs))
        else:
            self._outputter.process(DataFrames(ctx.inputs.values()))
        # TODO: output dummy to force cache to work, should we fix adagio?
        ctx.outputs["_0"] = ArrayDataFrame([], "_0:int")
