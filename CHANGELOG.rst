Change Log
##########

..
   All enhancements and patches to openedx_bulk_rerun_ext will be documented
   in this file.  It adheres to the structure of https://keepachangelog.com/ ,
   but in reStructuredText instead of Markdown (for ease of incorporation into
   Sphinx documentation and the PyPI description).

   This project adheres to Semantic Versioning (https://semver.org/).

.. There should always be an "Unreleased" section for changes pending release.

Unreleased
**********

0.2.6 – 2026-11-03
******************

Changed
========

* Defer runtime dependency versions to the host Ulmo edx-platform installation.
* Declare the directly imported edx-organizations dependency and move
  openedx-atlas from runtime requirements to development tooling.
* Replace runtime and test lockfiles with references to their input files,
  preserving those references during requirement compilation and upgrades.
* Replace remote common constraints with a local Ulmo snapshot and regenerate
  development, quality, documentation, and installer dependency locks.
* Use Django 5.2.11 for Ulmo tests and ancillary checks while retaining Django
  4.2 compatibility coverage and Python 3.12-compatible style checks.
* Run all CI matrix jobs and upload coverage from the Ulmo Django test job.

0.2.5 – 2026-10-27
******************

Added
=====

* Add the ``backfill_certificate_display`` management command to reconcile
  historical rerun courses with their batch certificate display settings.
  Preview by default, filter targets with ``--org``, and save changes with
  ``--apply --username`` using the resolved user for edit attribution.
* Process courses alphabetically and emit JSON audit output through stdout and
  Python logging, with summary changes grouped by status and per-course
  before/after values. Report skipped jobs, conflicting settings, and failures.

Fixed
=====

* Repair malformed certificate display strings using validated platform enum
  values and verify the course setting after saving to the modulestore.
* Report the effective ``before`` behavior using Studio's certificate settings
  validation, including availability dates, while retaining the raw stored value
  in detailed audit events.

0.2.4 – 2026-10-27
******************

Fixed
=====

* Persist the configured certificate display behavior to the target course when
  creating or activating its certificate configuration.
* Log the course key and certificate display behavior after the modulestore save
  succeeds. Allow save errors, including ImportError, to reach the settings task's
  error logging and retry handling.

0.1.0 – 2026-05-17
**********************************************

Added
=====

* First release on PyPI.
