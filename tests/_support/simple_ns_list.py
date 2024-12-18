from invoke import Collection, task


@task
def z_toplevel(c):
    pass


@task
def subtask(c):
    pass


ns = Collection(z_toplevel, Collection("a", Collection("b", subtask)))
