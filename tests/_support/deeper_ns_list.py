from invoke import Collection, task


@task
def toplevel(c):
    pass


@task
def subtask(c):
    pass


ns = Collection(
    toplevel, Collection("a", subtask, Collection("nother", subtask))
)
