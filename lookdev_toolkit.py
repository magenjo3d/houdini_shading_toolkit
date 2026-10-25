"""Lookdev Toolkit for Houdini.

A small standalone tool that:
  1. Reads the currently selected node(s) in Houdini.
  2. Recursively searches all children/descendants inside them.
  3. Lets you filter the results by node type and/or parameter name.
  4. Shows the matching nodes in a checkable list.
  5. Click any found node to inspect/edit *all* of its parameters at once
     in a friendly, type-aware table (checkboxes, spinners, color pickers,
     menus, ...) - each row can be applied to that node alone, or broadcast
     to every other checked node that shares the same parameter.
  6. Or copy the *entire* parameter interface from one "reference" node
     onto all the other checked nodes (optionally excluding some
     parameters, e.g. transforms).

Usage
-----
Running this file directly opens the tool immediately - paste its contents
into a shelf tool, load it in the Python Source Editor and hit Run/Apply,
or use File > Run Script.... No extra steps needed.

From the Python Shell / another script, instead do::

    import lookdev_toolkit
    lookdev_toolkit.show_lookdev_toolkit()

(the first ``import`` also opens the window; call ``show_lookdev_toolkit()``
again any time afterwards to reopen/refresh it.)

As a Houdini Python Panel:
    Create a new Python Panel interface, point it at this file/module and
    set the entry point function to ``createInterface``.

The module only depends on ``hou`` and Qt (PySide6/PySide2, whichever the
running Houdini version ships), so it can be dropped into any Houdini
install's ``python3.x libs`` / ``$HOUDINI_USER_PREF_DIR/scripts`` folder.
"""

import fnmatch

try:
    import hou
except ImportError:  # pragma: no cover - only importable inside Houdini
    hou = None

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:
    from PySide2 import QtCore, QtGui, QtWidgets


# ---------------------------------------------------------------------------
# Convenience presets for the "node type" filter combo box.
# ---------------------------------------------------------------------------
TYPE_PRESETS = {
    "(any)": "",
    "Principled Shader": "principledshader*",
    "Arnold Standard Surface": "arnold::standard_surface*",
    "Redshift Material": "redshift*material*,redshift_material*",
    "MaterialX Standard Surface": "mtlxstandard_surface*",
    "VRay Material": "vraymtl*,v_ray_mtl*",
    "Material Builder / Subnet": "subnet*,materialbuilder*",
}


# ---------------------------------------------------------------------------
# Core scanning helpers (no Qt dependency - reusable from scripts/tests).
# ---------------------------------------------------------------------------
def _as_glob(pattern):
    """Turn a plain substring into a '*substring*' glob unless the user
    already supplied wildcard characters themselves."""
    pattern = pattern.strip()
    if not pattern:
        return ""
    if any(ch in pattern for ch in "*?["):
        return pattern
    return "*{}*".format(pattern)


def _split_patterns(text):
    return [p.strip() for p in text.split(",") if p.strip()]


def node_matches_type(node, patterns):
    """True if node's type name (or full category::name) matches any of the
    comma separated glob patterns. Empty pattern list always matches."""
    if not patterns:
        return True
    type_name = node.type().name().lower()
    full_name = node.type().nameWithCategory().lower()
    for pat in patterns:
        glob = _as_glob(pat).lower()
        if fnmatch.fnmatch(type_name, glob) or fnmatch.fnmatch(full_name, glob):
            return True
    return False


def find_matching_parm_tuple(node, patterns):
    """Return the name of the first parmTuple on node whose name or label
    matches any of the comma separated glob patterns, or None."""
    if not patterns:
        return None
    for pt in node.parmTuples():
        template = pt.parmTemplate()
        name = template.name().lower()
        label = template.label().lower()
        for pat in patterns:
            glob = _as_glob(pat).lower()
            if fnmatch.fnmatch(name, glob) or fnmatch.fnmatch(label, glob):
                return template.name()
    return None


def get_selected_root_nodes():
    """Return the nodes currently selected in Houdini's network editor."""
    if hou is None:
        return ()
    return hou.selectedNodes()


def scan_children(roots, type_text="", parm_text="", recursive=True, include_roots=False):
    """Walk the children of ``roots`` and return a list of matching nodes.

    Parameters
    ----------
    roots : iterable of hou.Node
        The starting point(s), typically the user's current selection.
    type_text : str
        Comma separated glob pattern(s) matched against node type name.
    parm_text : str
        Comma separated glob pattern(s); only nodes that own a parm/parmTuple
        whose name or label matches are kept.
    recursive : bool
        If True, search all descendants (``allSubChildren``); otherwise only
        direct children.
    include_roots : bool
        If True, the root nodes themselves are also considered candidates.

    Returns
    -------
    list of (hou.Node, matched_parm_name_or_None)
    """
    type_patterns = _split_patterns(type_text)
    parm_patterns = _split_patterns(parm_text)

    seen = set()
    results = []
    for root in roots:
        candidates = []
        if include_roots:
            candidates.append(root)
        if recursive:
            candidates.extend(root.allSubChildren(top_down=True))
        else:
            candidates.extend(root.children())

        for node in candidates:
            path = node.path()
            if path in seen:
                continue
            if not node_matches_type(node, type_patterns):
                continue
            matched_parm = find_matching_parm_tuple(node, parm_patterns)
            if parm_patterns and matched_parm is None:
                continue
            seen.add(path)
            results.append((node, matched_parm))
    return results


# ---------------------------------------------------------------------------
# Parm copy / batch-set helpers.
# ---------------------------------------------------------------------------
def _copy_parm(src, dst):
    """Best-effort copy of a single hou.Parm value/expression/keyframes."""
    if dst.isLocked():
        return
    try:
        keyframes = src.keyframes()
    except hou.OperationFailed:
        keyframes = ()
    if keyframes:
        try:
            dst.setKeyframes(keyframes)
            return
        except hou.OperationFailed:
            pass
    try:
        expr = src.expression()
        lang = src.expressionLanguage()
        dst.setExpression(expr, language=lang)
        return
    except hou.OperationFailed:
        pass
    dst.set(src.eval())


def copy_all_parms(src_node, dst_node, exclude_patterns=None, include_spare=True):
    """Copy every matching parmTuple from src_node onto dst_node.

    Returns (copied_count, skipped_count, list_of_error_strings).
    """
    exclude_patterns = exclude_patterns or []
    copied = skipped = 0
    errors = []
    for src_pt in src_node.parmTuples():
        name = src_pt.name()
        if not include_spare and src_pt.isSpare():
            continue
        if any(fnmatch.fnmatch(name.lower(), _as_glob(pat).lower()) for pat in exclude_patterns):
            continue
        dst_pt = dst_node.parmTuple(name)
        if dst_pt is None or len(dst_pt) != len(src_pt):
            skipped += 1
            continue
        try:
            for s, d in zip(src_pt, dst_pt):
                _copy_parm(s, d)
            copied += 1
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            errors.append("{}: {}".format(name, exc))
            skipped += 1
    return copied, skipped, errors


def set_parm_tuple_value(node, parm_name, values, as_expression=False, expr_language=None):
    """Set (or set-expression-on) a parmTuple by name. Returns (ok, error)."""
    pt = node.parmTuple(parm_name)
    if pt is None:
        return False, "no parameter named '{}'".format(parm_name)
    if len(pt) != len(values):
        return False, "component count mismatch ({} expected, got {})".format(len(pt), len(values))
    try:
        if as_expression:
            lang = expr_language or hou.exprLanguage.Hscript
            for p, v in zip(pt, values):
                p.setExpression(str(v), language=lang)
        else:
            pt.set(values)
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# ---------------------------------------------------------------------------
# Dynamic value editor - builds the right Qt widgets for a given parmTemplate.
# ---------------------------------------------------------------------------
class ParmValueEditor(QtWidgets.QWidget):
    """A widget that adapts itself to a hou.ParmTemplate's type so the user
    can enter a new value (float/int/string/toggle/menu/color)."""

    def __init__(self, parent=None):
        super(ParmValueEditor, self).__init__(parent)
        self._layout = QtWidgets.QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._fields = []
        self._template = None
        self._placeholder = QtWidgets.QLabel("Detect a parameter to edit its value...")
        self._placeholder.setStyleSheet("color: gray;")
        self._layout.addWidget(self._placeholder)

    def clear(self):
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._fields = []
        self._template = None

    def build_for_template(self, template, initial_values=None):
        self.clear()
        self._template = template
        n = template.numComponents() if hasattr(template, "numComponents") else 1
        kind = template.type()
        initial_values = list(initial_values) if initial_values else [None] * n

        if kind == hou.parmTemplateType.Toggle:
            cb = QtWidgets.QCheckBox()
            cb.setChecked(bool(initial_values[0]) if initial_values[0] is not None else False)
            self._fields.append(cb)
            self._layout.addWidget(cb)

        elif kind == hou.parmTemplateType.Menu:
            combo = QtWidgets.QComboBox()
            items = template.menuItems()
            combo.addItems(template.menuLabels())
            if initial_values[0] is not None:
                # Ordered/int menus store an index; token menus (StringReplace
                # etc.) store the raw item string - handle both.
                idx = 0
                try:
                    idx = items.index(str(initial_values[0]))
                except ValueError:
                    try:
                        idx = int(initial_values[0])
                    except (TypeError, ValueError):
                        idx = 0
                combo.setCurrentIndex(idx)
            self._fields.append(combo)
            self._layout.addWidget(combo)

        elif kind == hou.parmTemplateType.String:
            menu_items = template.menuItems() if hasattr(template, "menuItems") else ()
            if menu_items:
                combo = QtWidgets.QComboBox()
                combo.addItems(template.menuLabels())
                combo.setEditable(True)
                if initial_values[0] is not None:
                    idx = combo.findText(str(initial_values[0]))
                    if idx >= 0:
                        combo.setCurrentIndex(idx)
                    else:
                        combo.setEditText(str(initial_values[0]))
                self._fields.append(combo)
                self._layout.addWidget(combo)
            else:
                for v in initial_values:
                    le = QtWidgets.QLineEdit(str(v) if v is not None else "")
                    self._fields.append(le)
                    self._layout.addWidget(le)

        elif kind in (hou.parmTemplateType.Float, hou.parmTemplateType.Int):
            is_color = kind == hou.parmTemplateType.Float and n in (3, 4)
            try:
                is_color = is_color and template.look() == hou.parmLook.ColorSquare
            except AttributeError:
                pass
            for i in range(n):
                if kind == hou.parmTemplateType.Float:
                    spin = QtWidgets.QDoubleSpinBox()
                    spin.setDecimals(4)
                else:
                    spin = QtWidgets.QSpinBox()
                spin.setRange(-1000000, 1000000)
                if initial_values[i] is not None:
                    spin.setValue(initial_values[i])
                self._fields.append(spin)
                self._layout.addWidget(spin)
            if is_color:
                pick_btn = QtWidgets.QPushButton("Pick...")
                pick_btn.clicked.connect(self._pick_color)
                self._layout.addWidget(pick_btn)
        else:
            lbl = QtWidgets.QLabel("Unsupported parameter type: {}".format(kind))
            lbl.setStyleSheet("color: orange;")
            self._layout.addWidget(lbl)

    def _pick_color(self):
        current = QtGui.QColor.fromRgbF(*[max(0.0, min(1.0, f.value())) for f in self._fields[:3]])
        color = QtWidgets.QColorDialog.getColor(current, self)
        if color.isValid():
            self._fields[0].setValue(color.redF())
            self._fields[1].setValue(color.greenF())
            if len(self._fields) > 2:
                self._fields[2].setValue(color.blueF())

    def values(self):
        """Return the current list of values, typed to match the template."""
        if self._template is None:
            return None
        kind = self._template.type()
        if kind == hou.parmTemplateType.Toggle:
            return [self._fields[0].isChecked()]
        if kind == hou.parmTemplateType.Menu:
            idx = self._fields[0].currentIndex()
            try:
                is_token_menu = self._template.menuType() != hou.menuType.Normal
            except AttributeError:
                is_token_menu = False
            if is_token_menu:
                return [self._template.menuItems()[idx]]
            return [idx]
        if kind == hou.parmTemplateType.String:
            return [self._fields[0].currentText() if isinstance(self._fields[0], QtWidgets.QComboBox)
                    else self._fields[0].text()]
        return [f.value() for f in self._fields]


# ---------------------------------------------------------------------------
# Main widget.
# ---------------------------------------------------------------------------
class LookdevToolkitWidget(QtWidgets.QWidget):

    def __init__(self, parent=None):
        super(LookdevToolkitWidget, self).__init__(parent)
        self.setWindowTitle("Lookdev Toolkit")
        self.resize(980, 620)

        self._roots = []
        self._results = []  # list of (hou.Node, matched_parm_name)
        self._reference_path = None
        self._inspected_path = None

        self._build_ui()
        self.rescan()

    # -- UI construction ---------------------------------------------------
    def _build_ui(self):
        main_layout = QtWidgets.QVBoxLayout(self)

        main_layout.addLayout(self._build_filter_bar())

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.addWidget(self._build_tree())
        splitter.addWidget(self._build_tabs())
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        main_layout.addWidget(splitter, 1)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setStyleSheet("color: gray;")
        main_layout.addWidget(self.status_label)

    def _build_filter_bar(self):
        layout = QtWidgets.QGridLayout()

        self.roots_label = QtWidgets.QLabel("Selection: (none)")
        layout.addWidget(self.roots_label, 0, 0, 1, 4)

        layout.addWidget(QtWidgets.QLabel("Node type contains:"), 1, 0)
        self.type_combo = QtWidgets.QComboBox()
        self.type_combo.setEditable(True)
        self.type_combo.addItems(list(TYPE_PRESETS.keys()))
        self.type_combo.currentTextChanged.connect(self._on_type_preset_changed)
        self._on_type_preset_changed(self.type_combo.currentText())
        layout.addWidget(self.type_combo, 1, 1)

        layout.addWidget(QtWidgets.QLabel("Parameter name contains:"), 1, 2)
        self.parm_filter_edit = QtWidgets.QLineEdit()
        self.parm_filter_edit.setPlaceholderText("e.g. basecolor, rough (optional)")
        layout.addWidget(self.parm_filter_edit, 1, 3)

        self.recursive_check = QtWidgets.QCheckBox("Search all children recursively")
        self.recursive_check.setChecked(True)
        layout.addWidget(self.recursive_check, 2, 0, 1, 2)

        self.include_roots_check = QtWidgets.QCheckBox("Include selected nodes themselves")
        layout.addWidget(self.include_roots_check, 2, 2, 1, 2)

        rescan_btn = QtWidgets.QPushButton("Rescan Selection")
        rescan_btn.clicked.connect(self.rescan)
        layout.addWidget(rescan_btn, 0, 4, 3, 1)

        return layout

    def _on_type_preset_changed(self, text):
        preset = TYPE_PRESETS.get(text)
        if preset is not None:
            # Replace the combo's edit text with the underlying glob pattern
            # without re-triggering this slot.
            self.type_combo.blockSignals(True)
            self.type_combo.setEditText(preset)
            self.type_combo.blockSignals(False)

    def _build_tree(self):
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Path", "Type", "Matched Parm"])
        self.tree.setColumnWidth(0, 160)
        self.tree.setColumnWidth(1, 260)
        self.tree.setColumnWidth(2, 180)
        self.tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self.tree.itemSelectionChanged.connect(self._on_tree_selection_changed)

        container = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(container)
        v.setContentsMargins(0, 0, 0, 0)

        btn_row = QtWidgets.QHBoxLayout()
        check_all_btn = QtWidgets.QPushButton("Check All")
        check_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        uncheck_all_btn = QtWidgets.QPushButton("Uncheck All")
        uncheck_all_btn.clicked.connect(lambda: self._set_all_checked(False))
        invert_btn = QtWidgets.QPushButton("Invert")
        invert_btn.clicked.connect(self._invert_checked)
        btn_row.addWidget(check_all_btn)
        btn_row.addWidget(uncheck_all_btn)
        btn_row.addWidget(invert_btn)
        btn_row.addStretch()
        self.count_label = QtWidgets.QLabel("0 nodes found")
        btn_row.addWidget(self.count_label)

        v.addLayout(btn_row)
        v.addWidget(self.tree)
        return container

    def _build_tabs(self):
        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._build_parameters_tab(), "Node Parameters")
        tabs.addTab(self._build_copy_tab(), "Copy All From Reference")
        return tabs

    def _build_parameters_tab(self):
        """Full parameter list/editor for whichever node is currently
        selected (clicked) in the tree on the left."""
        w = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(w)

        top_row = QtWidgets.QHBoxLayout()
        self.inspected_node_label = QtWidgets.QLabel("Node: (click a node in the list on the left)")
        self.inspected_node_label.setWordWrap(True)
        top_row.addWidget(self.inspected_node_label, 1)
        refresh_btn = QtWidgets.QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_parameter_table)
        top_row.addWidget(refresh_btn)
        layout.addLayout(top_row)

        filter_row = QtWidgets.QHBoxLayout()
        filter_row.addWidget(QtWidgets.QLabel("Filter:"))
        self.parm_table_filter_edit = QtWidgets.QLineEdit()
        self.parm_table_filter_edit.setPlaceholderText("Filter by parameter name or label...")
        self.parm_table_filter_edit.textChanged.connect(self._refresh_parameter_table)
        filter_row.addWidget(self.parm_table_filter_edit)
        self.spare_only_check = QtWidgets.QCheckBox("Spare only")
        self.spare_only_check.toggled.connect(self._refresh_parameter_table)
        filter_row.addWidget(self.spare_only_check)
        layout.addLayout(filter_row)

        self.broadcast_check = QtWidgets.QCheckBox(
            "Also apply to all checked nodes that have the same parameter"
        )
        layout.addWidget(self.broadcast_check)

        self.parm_table = QtWidgets.QTableWidget(0, 3)
        self.parm_table.setHorizontalHeaderLabels(["Parameter", "Value", ""])
        header = self.parm_table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        self.parm_table.verticalHeader().setVisible(False)
        self.parm_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.parm_table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        layout.addWidget(self.parm_table)

        return w


    def _build_copy_tab(self):
        w = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(w)

        self.reference_label = QtWidgets.QLabel("Reference node: (none - right-click a node in the list)")
        self.reference_label.setWordWrap(True)
        layout.addWidget(self.reference_label)

        form = QtWidgets.QFormLayout()
        self.exclude_edit = QtWidgets.QLineEdit("tx,ty,tz,rx,ry,rz,sx,sy,sz,name")
        form.addRow("Exclude parms (glob, comma):", self.exclude_edit)
        layout.addLayout(form)

        self.include_spare_check = QtWidgets.QCheckBox("Include spare parameters")
        self.include_spare_check.setChecked(True)
        layout.addWidget(self.include_spare_check)

        layout.addStretch()

        apply_btn = QtWidgets.QPushButton("Copy All Parameters To Checked Nodes")
        apply_btn.clicked.connect(self._apply_copy_from_reference)
        layout.addWidget(apply_btn)

        return w

    # -- scanning ------------------------------------------------------------
    def rescan(self):
        selected = get_selected_root_nodes()
        if selected:
            self._roots = list(selected)
        if not self._roots:
            self.roots_label.setText("Selection: (none) - select node(s) in Houdini and click Rescan")
            self._results = []
            self._populate_tree()
            return

        self.roots_label.setText(
            "Selection: " + ", ".join(n.path() for n in self._roots)
        )

        type_text = self.type_combo.currentText() if hasattr(self, "type_combo") else ""
        parm_text = self.parm_filter_edit.text() if hasattr(self, "parm_filter_edit") else ""
        recursive = self.recursive_check.isChecked() if hasattr(self, "recursive_check") else True
        include_roots = self.include_roots_check.isChecked() if hasattr(self, "include_roots_check") else False

        self._results = scan_children(
            self._roots,
            type_text=type_text,
            parm_text=parm_text,
            recursive=recursive,
            include_roots=include_roots,
        )
        self._populate_tree()
        self._set_status("Found {} node(s).".format(len(self._results)))

    def _populate_tree(self):
        self.tree.clear()
        for node, matched_parm in self._results:
            item = QtWidgets.QTreeWidgetItem([
                node.name(), node.path(), node.type().name(), matched_parm or ""
            ])
            item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
            item.setCheckState(0, QtCore.Qt.Checked)
            item.setData(0, QtCore.Qt.UserRole, node.path())
            self.tree.addTopLevelItem(item)
        self.count_label.setText("{} nodes found".format(len(self._results)))

    # -- tree helpers ---------------------------------------------------------
    def _all_items(self):
        return [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]

    def _set_all_checked(self, checked):
        state = QtCore.Qt.Checked if checked else QtCore.Qt.Unchecked
        for item in self._all_items():
            item.setCheckState(0, state)

    def _invert_checked(self):
        for item in self._all_items():
            new_state = QtCore.Qt.Unchecked if item.checkState(0) == QtCore.Qt.Checked else QtCore.Qt.Checked
            item.setCheckState(0, new_state)

    def _checked_nodes(self):
        nodes = []
        for item in self._all_items():
            if item.checkState(0) == QtCore.Qt.Checked:
                path = item.data(0, QtCore.Qt.UserRole)
                node = hou.node(path)
                if node is not None:
                    nodes.append(node)
        return nodes

    def _on_tree_selection_changed(self):
        item = self.tree.currentItem()
        if item is None:
            return
        self._inspected_path = item.data(0, QtCore.Qt.UserRole)
        self._refresh_parameter_table()

    def _on_tree_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            return
        menu = QtWidgets.QMenu(self)
        select_action = menu.addAction("Select In Houdini")
        reference_action = menu.addAction("Set As Reference Node")
        popup = getattr(menu, "exec_", menu.exec)
        action = popup(self.tree.viewport().mapToGlobal(pos))
        path = item.data(0, QtCore.Qt.UserRole)
        node = hou.node(path)
        if node is None:
            return
        if action == select_action:
            node.setSelected(True, clear_all_selected=True)
        elif action == reference_action:
            self._reference_path = path
            self.reference_label.setText("Reference node: {}".format(path))

    # -- node parameters tab ---------------------------------------------------
    def _refresh_parameter_table(self):
        self.parm_table.setRowCount(0)
        node = hou.node(self._inspected_path) if self._inspected_path else None
        if node is None:
            self.inspected_node_label.setText("Node: (click a node in the list on the left)")
            return
        self.inspected_node_label.setText("Node: {}  ({})".format(node.path(), node.type().name()))

        text_filter = self.parm_table_filter_edit.text().strip().lower()
        spare_only = self.spare_only_check.isChecked()

        for pt in node.parmTuples():
            template = pt.parmTemplate()
            if template.isHidden():
                continue
            if spare_only and not pt.isSpare():
                continue
            name = template.name()
            label = template.label()
            if text_filter and text_filter not in name.lower() and text_filter not in label.lower():
                continue

            row = self.parm_table.rowCount()
            self.parm_table.insertRow(row)

            name_item = QtWidgets.QTableWidgetItem(label)
            name_item.setFlags(QtCore.Qt.ItemIsEnabled)
            name_item.setToolTip(name)
            self.parm_table.setItem(row, 0, name_item)

            try:
                values = [p.eval() for p in pt]
                editor = ParmValueEditor()
                editor.build_for_template(template, values)
                self.parm_table.setCellWidget(row, 1, editor)
            except Exception as exc:  # noqa: BLE001 - keep unsupported parms visible, not fatal
                self.parm_table.setCellWidget(row, 1, QtWidgets.QLabel("(unsupported: {})".format(exc)))

            apply_btn = QtWidgets.QPushButton("Apply")
            apply_btn.clicked.connect(lambda _checked=False, r=row, n=name: self._apply_parameter_row(r, n))
            self.parm_table.setCellWidget(row, 2, apply_btn)

        self.parm_table.resizeRowsToContents()

    def _apply_parameter_row(self, row, parm_name):
        node = hou.node(self._inspected_path) if self._inspected_path else None
        if node is None:
            self._set_status("Inspected node no longer exists.", error=True)
            return
        editor = self.parm_table.cellWidget(row, 1)
        if not isinstance(editor, ParmValueEditor):
            self._set_status("'{}' has an unsupported type and cannot be edited here.".format(parm_name), error=True)
            return
        values = editor.values()
        if values is None:
            return

        targets = [node]
        if self.broadcast_check.isChecked():
            targets.extend(n for n in self._checked_nodes() if n.path() != node.path())

        applied = 0
        errors = []
        with hou.undos.group("Lookdev Toolkit: Set '{}'".format(parm_name)):
            for target in targets:
                ok, err = set_parm_tuple_value(target, parm_name, values)
                if ok:
                    applied += 1
                else:
                    errors.append("{}: {}".format(target.path(), err))

        msg = "Set '{}' on {}/{} node(s).".format(parm_name, applied, len(targets))
        if errors:
            msg += " {} error(s), see Houdini console.".format(len(errors))
            for e in errors:
                print("[Lookdev Toolkit] " + e)
        self._set_status(msg, error=bool(errors) and applied == 0)

    # -- copy-from-reference tab -------------------------------------------
    def _apply_copy_from_reference(self):
        if not self._reference_path:
            self._set_status("Right-click a node in the list and choose 'Set As Reference Node' first.", error=True)
            return
        src_node = hou.node(self._reference_path)
        if src_node is None:
            self._set_status("Reference node no longer exists: {}".format(self._reference_path), error=True)
            return

        targets = [n for n in self._checked_nodes() if n.path() != self._reference_path]
        if not targets:
            self._set_status("No checked target nodes (other than the reference) to copy to.", error=True)
            return

        exclude_patterns = _split_patterns(self.exclude_edit.text())
        include_spare = self.include_spare_check.isChecked()

        total_copied = total_skipped = 0
        all_errors = []
        with hou.undos.group("Lookdev Toolkit: Copy parms from {}".format(src_node.name())):
            for dst in targets:
                copied, skipped, errors = copy_all_parms(
                    src_node, dst, exclude_patterns=exclude_patterns, include_spare=include_spare
                )
                total_copied += copied
                total_skipped += skipped
                all_errors.extend(errors)

        msg = "Copied parameters to {} node(s): {} copied, {} skipped.".format(
            len(targets), total_copied, total_skipped
        )
        if all_errors:
            msg += " {} error(s), see Houdini console.".format(len(all_errors))
            for e in all_errors:
                print("[Lookdev Toolkit] " + e)
        self._set_status(msg)

    # -- misc -----------------------------------------------------------------
    def _set_status(self, text, error=False):
        self.status_label.setText(text)
        self.status_label.setStyleSheet("color: {};".format("#d9534f" if error else "gray"))


# ---------------------------------------------------------------------------
# Launchers.
# ---------------------------------------------------------------------------
_toolkit_window = None


def show_lookdev_toolkit():
    """Open the Lookdev Toolkit in a standalone floating window, or close
    and reopen it if one is already open. This is the function to call
    from a shelf tool, the Python Source Editor, or the Python Shell::

        import lookdev_toolkit
        lookdev_toolkit.show_lookdev_toolkit()
    """
    global _toolkit_window
    if hou is None:
        raise RuntimeError(
            "Lookdev Toolkit must be run inside Houdini ('hou' module not found)."
        )

    if _toolkit_window is not None:
        try:
            _toolkit_window.close()
        except RuntimeError:
            pass  # underlying Qt widget was already destroyed

    parent = hou.qt.mainWindow()
    _toolkit_window = LookdevToolkitWidget(parent)
    _toolkit_window.setWindowFlags(QtCore.Qt.Window)
    _toolkit_window.show()
    _toolkit_window.raise_()
    _toolkit_window.activateWindow()
    return _toolkit_window


def createInterface():
    """Entry point used by Houdini Python Panels."""
    return LookdevToolkitWidget()


# Houdini does not reliably set __name__ to "__main__" when a script is run
# from a shelf tool, the Python Source Editor's Run/Apply button, or
# File > Run Script... (that is a plain-CPython convention, not how Houdini
# executes embedded scripts). Relying on that guard means the window would
# silently never appear in those contexts, so instead we open the tool
# unconditionally whenever this module is loaded inside Houdini. Re-running
# `import lookdev_toolkit` after the first time is a no-op (Python caches
# modules); call show_lookdev_toolkit() directly to reopen it afterwards.
if hou is not None:
    show_lookdev_toolkit()
