"""Version du moteur.

RÈGLE : on incrémente ENGINE_VERSION à chaque patch qui touche au moteur (engine, stats, models,
venues/*, windows, logique de main/db). Au démarrage, si la version change et que la DB n'est pas
protégée (maintenance.py), la DB est wipée. Un patch API/UI seul ne change pas la version.
"""
ENGINE_VERSION = "0.7.1"
