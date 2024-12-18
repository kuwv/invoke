from subspace import module

from invoke import Collection, call, task


@task
def top_pre(c):
    pass


@task(call(top_pre))
def toplevel(c):
    pass


ns = Collection(module, toplevel)
