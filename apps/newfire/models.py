"""
This file defines the database models
"""

from pydal.validators import *

from .common import Field, auth, db
from .musicbrainz import (
    create_discovered_indexes,
    create_link_lookup_indexes,
    define_discovered_link_table,
    define_link_lookup_table,
)

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

# Streaming links this app found that MusicBrainz does not carry yet.
#
# This is the second thing here that would hurt to lose, and the reason it is
# not in the cache: a sync rebuilds a release's URL set wholesale, so a link
# written there survives only until that label is next refreshed. The schema and
# the projection that copies these into the cache both live in
# musicbrainz/discovered.py; see its module docstring for why the indirection
# earns its keep.
define_discovered_link_table(db)
create_discovered_indexes(db)

# What has already been searched for, so the weekly sweep's cost tracks new
# releases rather than the whole back catalogue.
define_link_lookup_table(db)
create_link_lookup_indexes(db)

db.commit()
