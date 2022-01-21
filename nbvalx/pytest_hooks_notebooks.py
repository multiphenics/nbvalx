# Copyright (C) 2022 by the nbvalx authors
#
# This file is part of nbvalx.
#
# SPDX-License-Identifier: BSD-3-Clause
"""
Utility functions to be used in pytest configuration file for notebooks tests.

nbvalx changes the default behavior of nbval in the following three ways:
    1) Users may start a ipyparallel Cluster and run notebooks tests in parallel
    2) New markers are introduced to mark cells as xfail
    3) Handle selective cell run using tags introduced in magics.py

See also: https://github.com/pytest-dev/pytest/blob/main/src/_pytest/hookspec.py for type hints.
"""

import os
import typing

try:
    import copy
    import fnmatch

    import _pytest.config
    import _pytest.main
    import _pytest.nodes
    import _pytest.runner
    import nbformat
    import nbval.plugin
    import py
    import pytest
except ImportError:  # pragma: no cover
    addoption = None
    sessionstart = None
    collect_file = None
    runtest_setup = None
    runtest_makereport = None
    runtest_teardown = None
else:
    def addoption(parser: _pytest.main.Parser) -> None:
        """Add options to set the number of processes and tag actions."""
        # Number of processors
        parser.addoption("--np", action="store", type=int, default=1, help="Number of MPI processes to use")
        assert (
            not ("OMPI_COMM_WORLD_SIZE" in os.environ  # OpenMPI
                 or "MPI_LOCALNRANKS" in os.environ)), (  # MPICH
            "Please do not start pytest under mpirun. Use the --np pytest option.")
        # Tag actions
        parser.addoption(
            "--tag-action", type=str, default="collect-notebooks", help="action on notebooks with tags")
        parser.addoption("--tag-collapse", action="store_true", help="collapse notebook to active tag")

    def sessionstart(session: _pytest.main.Session) -> None:
        """Parameterize jupyter notebooks based on available tags."""
        tag_action = session.config.option.tag_action
        tag_collapse = session.config.option.tag_collapse
        assert tag_action in ("create-notebooks", "collect-notebooks")
        # Patch default norecursepatterns so that the files we create will not get ignored
        norecursepatterns = session.config.getini("norecursedirs")
        assert ".*" in norecursepatterns
        norecursepatterns.remove(".*")
        norecursepatterns.append(".ipynb_checkpoints")
        norecursepatterns.append(".ipynb_mpi")
        norecursepatterns.append(".ipynb_tags")
        # Create tags based on existing notebooks
        dirpaths = list()
        for arg in session.config.args:
            dirpath, _ = _pytest.main.resolve_collection_argument(session.config.invocation_params.dir, arg)
            dirpaths.append(str(dirpath))
        for dirpath in dirpaths:
            files = list()
            if os.path.isdir(dirpath):
                for direntry in _pytest.pathlib.visit(dirpath, session._recurse):
                    if direntry.is_file():
                        files.append(str(direntry.path))
            else:  # pragma: no cover
                assert os.path.isfile(dirpath)
                files.append(dirpath)
            for filepath in files:
                if fnmatch.fnmatch(filepath, "**/*.ipynb") and not fnmatch.fnmatch(
                        filepath, "**/.ipynb_tags/*.ipynb"):
                    # Read in notebook
                    with open(filepath) as f:
                        nb = nbformat.read(f, as_version=4)
                    # Determine if the run_if extension is used
                    run_if_loaded = False
                    allowed_tags = []
                    for cell in nb.cells:
                        if cell.cell_type == "code":
                            if cell.source.startswith("%load_ext nbvalx"):
                                run_if_loaded = True
                                assert len(cell.source.splitlines()) == 1, (
                                    "Use a standalone cell for %load_ext nbvalx")
                            elif cell.source.startswith("%register_run_if_allowed_tags"):
                                assert run_if_loaded
                                lines = cell.source.splitlines()
                                assert len(lines) == 1, (
                                    "Use a standalone cell for %register_run_if_allowed_tags")
                                line = lines[0].replace("%register_run_if_allowed_tags ", "")
                                allowed_tags = [tag.strip() for tag in line.split(",")]
                    if not run_if_loaded or len(allowed_tags) == 0:
                        continue
                    # Create temporary copies for each allowed tag
                    for tag in allowed_tags:
                        # Determine what will be the new notebook name and path
                        tags_dir = os.path.join(
                            os.path.dirname(filepath), ".ipynb_tags", f"collapse_{tag_collapse}")
                        ipynb_path = os.path.join(
                            tags_dir, os.path.basename(filepath).replace(".ipynb", f"[{tag}].ipynb"))
                        # Replace tag and, if collapsing notebooks, strip cells with other tags
                        cells_tag = list()
                        for cell in nb.cells:
                            if cell.cell_type == "code":
                                if (cell.source.startswith("%load_ext nbvalx")
                                        or cell.source.startswith("%register_run_if_allowed_tags")):
                                    if not tag_collapse:
                                        cells_tag.append(cell)
                                elif cell.source.startswith("%register_run_if_current_tag"):
                                    assert len(cell.source.splitlines()) == 1, (
                                        "Use a standalone cell for %register_run_if_current_tag")
                                    if not tag_collapse:
                                        cell_tag = copy.deepcopy(cell)
                                        cell_tag.source = f"%register_run_if_current_tag {tag}"
                                        cells_tag.append(cell_tag)
                                elif "%%run_if" in cell.source:
                                    if tag_collapse:
                                        lines = cell.source.splitlines()
                                        magic_line_index = 0
                                        while not lines[magic_line_index].startswith("%%run_if"):
                                            magic_line_index += 1
                                        assert magic_line_index < len(lines)
                                        line = lines[magic_line_index].replace("%%run_if ", "")
                                        allowed_tags = [tag.strip() for tag in line.split(",")]
                                        if tag in allowed_tags:
                                            cell_tag = copy.deepcopy(cell)
                                            lines.remove(lines[magic_line_index])
                                            cell_tag.source = "\n".join(lines)
                                            cells_tag.append(cell_tag)
                                    else:
                                        cells_tag.append(cell)
                                elif cell.source.startswith("__notebook_name__"):
                                    assert len(cell.source.splitlines()) == 1, (
                                        "Use a standalone cell for __notebook_name__")
                                    cell_tag = copy.deepcopy(cell)
                                    notebook_name_tag = os.path.relpath(
                                        ipynb_path, os.path.join(os.path.dirname(filepath), ".ipynb_tags"))
                                    cell_tag.source = f'__notebook_name__ = "{notebook_name_tag}"'
                                    cells_tag.append(cell_tag)
                                else:
                                    cells_tag.append(cell)
                            else:
                                cells_tag.append(cell)
                        # Attach cells to a copy of the notebook
                        nb_tag = copy.deepcopy(nb)
                        nb_tag.cells = cells_tag
                        # Write modified notebook to a temporary file
                        os.makedirs(tags_dir, exist_ok=True)
                        with open(ipynb_path, "w") as f:
                            nbformat.write(nb_tag, str(ipynb_path))
            # Allow collecting the create copies, but only for the current value of tag_collapse
            norecursepatterns.remove(".ipynb_tags")
            norecursepatterns.append(os.path.join(".ipynb_tags", f"collapse_{not tag_collapse}"))

    def collect_file(path: py.path.local, parent: _pytest.nodes.Collector) -> nbval.plugin.IPyNbFile:
        """Collect IPython notebooks using a custom pytest nbval hook."""
        opt = parent.config.option
        assert not opt.nbval, "--nbval is implicitly enabled, do not provide it on the command line"
        if path.fnmatch("**/*.ipynb"):
            if opt.np > 1:
                # Read in notebook
                with open(path) as f:
                    nb = nbformat.read(f, as_version=4)
                # Add the %%px magic to every existing cell
                for cell in nb.cells:
                    if cell.cell_type == "code":
                        cell.source = "%%px\n" + cell.source
                # Add a cell on top to start a new ipyparallel cluster
                cluster_start_code = f"""import ipyparallel as ipp

cluster = ipp.Cluster(engines="MPI", profile="mpi", n={opt.np})
cluster.start_and_connect_sync()"""
                cluster_start_cell = nbformat.v4.new_code_cell(cluster_start_code)
                cluster_start_cell.id = "cluster_start"
                nb.cells.insert(0, cluster_start_cell)
                # Add a further cell on top to disable garbage collection
                gc_disable_code = """%%px
import gc

gc.disable()"""
                gc_disable_cell = nbformat.v4.new_code_cell(gc_disable_code)
                gc_disable_cell.id = "gc_disable"
                nb.cells.insert(1, gc_disable_cell)
                # Add a cell at the end to re-enable garbage collection
                gc_enable_code = """%%px
gc.enable()
gc.collect()"""
                gc_enable_cell = nbformat.v4.new_code_cell(gc_enable_code)
                gc_enable_cell.id = "gc_enable"
                nb.cells.append(gc_enable_cell)
                # Add a cell at the end to stop the ipyparallel cluster
                cluster_stop_code = """cluster.stop_cluster_sync()"""
                cluster_stop_cell = nbformat.v4.new_code_cell(cluster_stop_code)
                cluster_stop_cell.id = "cluster_stop"
                nb.cells.append(cluster_stop_cell)
                # Write modified notebook to a temporary file
                mpi_dir = os.path.join(path.dirname, ".ipynb_mpi")
                os.makedirs(mpi_dir, exist_ok=True)
                ipynb_path = path.new(dirname=mpi_dir)
                with open(ipynb_path, "w") as f:
                    nbformat.write(nb, str(ipynb_path))
            else:  # pragma: no cover
                ipynb_path = path
            if opt.tag_action != "create-notebooks":
                return nbval.plugin.IPyNbFile.from_parent(parent, fspath=ipynb_path)

    def runtest_setup(item: _pytest.nodes.Item) -> None:
        """Insert skips on cell failure."""
        # Do the normal setup
        item.setup()
        # If previous cells in a notebook failed skip the rest of the notebook
        if hasattr(item, "_force_skip"):
            if not hasattr(item.cell, "id") or item.cell.id not in ("gc_enable", "cluster_stop"):
                pytest.skip("A previous cell failed")

    def runtest_makereport(item: _pytest.nodes.Item, call: _pytest.runner.CallInfo[None]) -> None:
        """Determine whether the current cell failed or not."""
        if call.when == "call":
            if call.excinfo:
                source = item.cell.source
                lines = source.splitlines()
                while len(lines) > 1 and lines[0].startswith("%"):
                    lines = lines[1:]
                if len(lines) > 1 and lines[0].startswith("# PYTEST_XFAIL"):
                    xfail_line = lines[0]
                    xfail_comment = xfail_line.replace("# ", "")
                    xfail_marker, xfail_reason = xfail_comment.split(": ")
                    assert xfail_marker in (
                        "PYTEST_XFAIL", "PYTEST_XFAIL_IN_PARALLEL",
                        "PYTEST_XFAIL_AND_SKIP_NEXT", "PYTEST_XFAIL_IN_PARALLEL_AND_SKIP_NEXT")
                    if (xfail_marker in ("PYTEST_XFAIL", "PYTEST_XFAIL_AND_SKIP_NEXT")
                        or (xfail_marker in ("PYTEST_XFAIL_IN_PARALLEL", "PYTEST_XFAIL_IN_PARALLEL_AND_SKIP_NEXT")
                            and item.config.option.np > 1)):
                        # This failure was expected: report the reason of xfail.
                        call.excinfo._excinfo = (
                            call.excinfo._excinfo[0],
                            pytest.xfail.Exception(xfail_reason.capitalize()),
                            call.excinfo._excinfo[2])
                    if xfail_marker in ("PYTEST_XFAIL_AND_SKIP_NEXT", "PYTEST_XFAIL_IN_PARALLEL_AND_SKIP_NEXT"):
                        # The failure, even though expected, forces the rest of the notebook to be skipped.
                        item._force_skip = True
                else:  # pragma: no cover
                    # An unexpected error forces the rest of the notebook to be skipped.
                    item._force_skip = True

    def runtest_teardown(item: _pytest.nodes.Item, nextitem: typing.Optional[_pytest.nodes.Item]) -> None:
        """Propagate cell failure."""
        # Do the normal teardown
        item.teardown()
        # Inform next cell of the notebook of failure of any previous cells
        if hasattr(item, "_force_skip"):
            if nextitem is not None and nextitem.name != "Cell 0":
                nextitem._force_skip = True
