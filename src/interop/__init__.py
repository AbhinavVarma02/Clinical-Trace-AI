"""Optional, isolated adapters for external clinical data sources.

Nothing in the existing application imports this package. Each adapter is
opt-in, runs from a local command line, and writes only to its own isolated
workspace, never to the default runtime database.
"""
