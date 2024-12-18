from invoke import Collection, ctask


@ctask
def go(c):
    c.run("false")  # Ensures a kaboom if mocking fails


ns = Collection(go)
ns.configure({"run": {"echo": True}})
