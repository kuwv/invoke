from invoke import Collection, task


@task
def dummy(c):
    pass


ns = Collection(dummy, Collection("subcollection"))
