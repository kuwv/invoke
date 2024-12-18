from invoke import Collection, task


@task
def mytask(c):
    assert c.outer.inner.hooray == "yml"


ns = Collection(mytask)
