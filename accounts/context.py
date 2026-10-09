from . import demo


def demo_context(request):
    """Stellt Demo-Angaben für Banner und Hinweise bereit (nur für Demo-Konten)."""
    u = getattr(request, "user", None)
    if not demo.is_demo(u):
        return {}
    return {"demo": {"hours": demo.hours_left(u), "runs": demo.runs_used(u), "runs_max": demo.MAX_RUNS,
                     "ai_left": demo.ai_left(u), "ai_max": demo.AI_LIMIT}}
