# hunter

The job-hunting service (company scraping, job matching, AI analysis,
scheduled runs) as a package for a [RayServer](https://github.com/camilo-rojas/rayserver)
deployment. It runs inside fin's server today
(`class_path: hunter.services.jobhunter_service.JobHunterService` in fin's
`config/services/jobhunter.yaml`) and can become its own RayServer
application later without code changes. Formerly part of RobotRay2
(RobotRay ADR-029); design history in `docs/ADR005-*`.

`src/hunter/data_map.py` declares its data to RayServer (shared tables, CV
files in backups). hunter imports nothing from fin, and fin nothing from
hunter.
