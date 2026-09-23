"""`GuardedSession` — the single chokepoint through which every action flows.

policy.check -> lease.check -> surface.act -> evidence.emit. Discovery and replay are drivers
on top of this; neither can reach the surface directly.
"""
