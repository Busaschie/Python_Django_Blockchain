from django.contrib import admin

from .models import BacktestRun, ExchangeBlock


@admin.register(BacktestRun)
class BacktestRunAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "chain", "strategy", "timeframe", "exchange", "status", "created_at")
    list_filter = ("chain", "strategy", "status", "exchange")
    search_fields = ("owner__username",)
    readonly_fields = ("created_at",)


admin.site.register(ExchangeBlock)
