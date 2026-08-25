"""
This file defines the database models
"""

from pydal.validators import *

from .common import Field, auth, db

# MusicBrainz itself is deliberately not modelled with pydal. The mirror is
# queried with raw SQL in musicbrainz/sources.py, and everything the app renders
# comes from the SQLite cache whose schema lives in musicbrainz/cache.py. The
# mirror connection is also optional -- absent entirely when MB_SOURCE is
# "webservice" -- so defining tables against it here would make the app fail to
# load in that configuration.

# Labels a user follows.
#
# Only the MBID is stored: the label's name and releases live in the cache,
# which is rebuildable and must stay disposable. This table is user data and is
# the one thing here that would actually hurt to lose.
db.define_table(
    "tracked_label",
    Field("label_gid", requires=IS_NOT_EMPTY()),
    auth.signature,
)

# One row per user per label. Two clicks on a follow button arrive together
# often enough that the database, not the handler, should be the one saying no.
db.executesql(
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_tracked_label "
    "ON tracked_label (created_by, label_gid)"
)

db.commit()
