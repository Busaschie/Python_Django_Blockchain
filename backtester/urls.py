from django.urls import path
from . import views

urlpatterns = [
    path("", views.dashboard, name="index"),
    path("run/<int:pk>/", views.dashboard, name="detail"),
    path("vergleich/<str:batch>/", views.dashboard, name="compare"),
    path("loeschen/<int:pk>/", views.delete_run, name="delete"),
    path("kommentar/<int:pk>/", views.ai_comment, name="ai_comment"),
    path("export/<int:pk>/trades.csv", views.export_trades, name="export_trades"),
    path("export/<int:pk>/auswertung.pdf", views.export_pdf, name="export_pdf"),
    path("export/bericht.pdf", views.export_report, name="export_report"),
    path("teilen/<int:pk>/", views.share_toggle, name="share_toggle"),
    path("geteilt/<str:token>/", views.shared, name="shared"),
    path("geteilt/<str:token>/trades.csv", views.shared_trades, name="shared_trades"),
    path("geteilt/<str:token>/auswertung.pdf", views.shared_pdf, name="shared_pdf"),
    path("favorit/<int:pk>/", views.favorite_toggle, name="favorite_toggle"),
    path("tags/<int:pk>/", views.tags_set, name="tags_set"),
    path("vorlagen/<int:pk>/loeschen/", views.template_delete, name="template_delete"),
    path("signal/<int:pk>/pruefen/", views.signal_refresh, name="signal_refresh"),
    path("signal/<int:pk>/mail/", views.signal_toggle, name="signal_toggle"),
    path("signale/pruefen/", views.signal_check, name="signal_check"),
    path("status/", views.status, name="status"),
]
