# Fugue

[![GitHub release](https://img.shields.io/github/release/fugue-project/fugue.svg)](https://GitHub.com/fugue-project/fugue)
[![PyPI pyversions](https://img.shields.io/pypi/pyversions/fugue.svg)](https://pypi.python.org/pypi/fugue/)
[![PyPI license](https://img.shields.io/pypi/l/fugue.svg)](https://pypi.python.org/pypi/fugue/)
[![PyPI version](https://badge.fury.io/py/fugue.svg)](https://pypi.python.org/pypi/fugue/)
[![Coverage Status](https://coveralls.io/repos/github/fugue-project/fugue/badge.svg)](https://coveralls.io/github/fugue-project/fugue)
[![Doc](https://readthedocs.org/projects/fugue/badge)](https://fugue.readthedocs.org)

[Join Fugue-Project on Slack](https://join.slack.com/t/fugue-project/shared_invite/zt-he6tcazr-OCkj2GEv~J9UYoZT3FPM4g)

Fugue is a pure abstraction layer that adapts to different computing frameworks
such as Spark and Dask. It is to unify the core concepts of distributed computing and
to help you decouple your logic from specific computing frameworks.

## Installation
```
pip install fugue
```

Fugue has these extras:
* **sql**: to support [Fugue SQL](https://fugue-tutorials.readthedocs.io/en/latest/tutorials/sql.html)
* **spark**: to support Spark as the [ExecutionEngine](https://fugue-tutorials.readthedocs.io/en/latest/tutorials/execution_engine.html)
* **dask**: to support Dask as the [ExecutionEngine](https://fugue-tutorials.readthedocs.io/en/latest/tutorials/execution_engine.html)

For example a common use case is:
```
pip install fugue[sql,spark]
```


## Docs and Tutorials

To read the complete static docs, [click here](https://fugue.readthedocs.org)

The best way to start is to go through the tutorials. We have the tutorials in an interactive notebook environent.

### Run the tutorial using binder:
[![Binder](https://mybinder.org/badge_logo.svg)](https://mybinder.org/v2/gh/fugue-project/tutorials/master)

**But it runs slow on binder**, the machine on binder isn't powerful enough for
a distributed framework such as Spark. Parallel executions can become sequential, so some of the
performance comparison examples will not give you the correct numbers.

### Run the tutorial using docker

Alternatively, you should get decent performance if running its docker image on your own machine:

```
docker run -p 8888:8888 fugueproject/tutorials:latest
```
