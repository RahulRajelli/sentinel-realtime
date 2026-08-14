"""SentinelAgent realtime tier.

Consumes live MAVLink, converts it to dataflash-shaped records with flightdx's adapter,
and runs the same detectors the file path uses. flightdx is a dependency and is never
edited from here -- detector changes belong in ardupilot-log-analyzer with its tests.
"""
