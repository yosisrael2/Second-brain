"""Google-family connectors (gmail, gcal, gdrive, gtasks).

``base.GoogleConnector`` carries everything the Google adapters share:
OAuth creds, scope union, service factory, error translation, pagination.
Adapters subclass it and only implement ``fetch()`` (+ attachment fetch).
"""
