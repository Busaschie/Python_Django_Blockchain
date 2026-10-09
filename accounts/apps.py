from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "accounts"

    def ready(self):
        from django.contrib.auth.signals import user_logged_out

        def drop_demo_user(sender, user=None, **kw):
            """Beim Abmelden wird ein Demo-Konto sofort gelöscht (gibt den Platz frei, Daten sind weg)."""
            from . import demo
            if user is not None and user.pk and demo.is_demo(user):
                user.delete()

        user_logged_out.connect(drop_demo_user, dispatch_uid="accounts-drop-demo-user")
