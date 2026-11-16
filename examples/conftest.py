"""What `python -m pytest` leaves out under examples/ (the test options are the repository root's conftest.py's): a
Harbor benchmark's prepared task set (`<folder>/utils/tasks`, the link `python -m hermit.harbor.dataset prepare`
writes), whose tests are the benchmark's own and run in its task's container, and a run's folder (`runs/`, the
output_dir beside an example's configs, at any depth), which holds what its trials and learners wrote."""

collect_ignore_glob = ["*/utils/tasks", "*/runs"]      # fnmatch's `*` takes a `/` too: any depth
