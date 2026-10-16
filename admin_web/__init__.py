"""Administrator web console for nutrition review."""


def create_app(config=None):
    from admin_web.app import create_app as factory
    return factory(config)


__all__ = ["create_app"]
