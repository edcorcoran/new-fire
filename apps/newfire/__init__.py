# check compatibility
import py4web

assert py4web.check_compatible("1.20190709.1")

# by importing controllers you expose the actions defined in it
from . import controllers

# re-exported at package level; py4web tooling looks for `db` here
from .models import db

# import the scheduler
from .tasks import scheduler

# optional parameters
__version__ = "0.1"
__author__ = "Ed Corcoran"
__license__ = "AGPL-3.0-or-later"
