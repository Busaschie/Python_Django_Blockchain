def de(x, nd=2) -> str:
    """Zahl mit deutschem Dezimalkomma fuer Texte aus Python (Templates formatieren selbst)."""
    return f"{x:.{nd}f}".replace(".", ",")
