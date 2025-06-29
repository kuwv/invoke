from invoke import Collection, task


@task
def release(c):
    print('release', c.task.parent.get_collection('.'))
    print('release', c.task.parent.get_collection('ns'))
    print('release', c.task.parent.get_collection('ns.test'))
    # c.run("python setup.py sdist register upload")


ns = Collection('ns')
ns.add_task(release)


@task
def pytest(c):
    print('-----', c.task.parent, type(c.task.parent.tasks))
    for x in c.task.parent.tasks:
        print('---', type(x))
    c.doctest()
    print('path', c.task.parent.path)
    print(c.task.parent.get_collection('..docs'))
    ns_ctx = c('ns')
    assert ns_ctx.name == 'ns'
    print(type(ns_ctx))
    c.run("echo pytest-running")


@task
def doctest(c):
    c.run("echo doctest-running")


test = Collection('test')
test.add_task(pytest, 'pytest')
test.add_task(doctest, 'doctest')
ns.add_collection(test)


@task
def build_docs(c):
    c.run("echo build-docs")


@task
def clean_docs(c):
    c.run("rm -rf docs/_build")


docs = Collection('docs')
docs.add_task(build_docs, 'build')
docs.add_task(clean_docs, 'clean')
ns.add_collection(docs)

# print('ns.parent', ns.parent)
# print('docs.parent', docs.parent)
# print('docs.path', docs.path)
#
# for x in ns:
#     print('here', x)
#
# for x in reversed(docs):
#     print('rev', x)
