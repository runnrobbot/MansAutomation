"""Profile manager panel."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from mansautomation.core.container import Container
from mansautomation.core.events import EventBus
from mansautomation.core.exceptions import ProfileNotFoundError
from mansautomation.core.models import (
    Address,
    Attendee,
    BankInfo,
    LoginCredentials,
    Profile,
    generate_id,
)
from mansautomation.gui.widgets import (
    CardWidget,
    configure_form,
    make_danger_button,
    make_form_row,
    make_ghost_button,
    make_primary_button,
    make_scroll_container,
    make_section_header,
    make_title,
)
from mansautomation.profiles.manager import PROFILES_TOPIC, ProfileManager
from mansautomation.utils.async_qt import run_async


class ProfilePanel(QWidget):
    profile_selected = pyqtSignal(str)
    profiles_loaded = pyqtSignal(list)

    def __init__(self, container: Container, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._container = container
        self._profile_manager: ProfileManager = container.resolve(ProfileManager)
        self._current: Profile | None = None
        self._suppress_select = False

        self._build_ui()
        bus: EventBus = container.resolve(EventBus)
        bus.subscribe(PROFILES_TOPIC, self._on_profiles_changed)

        self.profiles_loaded.connect(self._render_profiles)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(14)
        outer.addWidget(make_title("Profiles"))
        outer.addWidget(
            make_section_header(
                "Reusable autofill datasets stored encrypted on this device."
            )
        )

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(8)

        splitter.addWidget(self._build_list_panel())
        splitter.addWidget(self._build_editor_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 760])
        outer.addWidget(splitter, stretch=1)

    def _build_list_panel(self) -> QWidget:
        container = QWidget()
        container.setMinimumWidth(260)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        layout.addWidget(make_section_header("Profiles"))
        self._list = QListWidget()
        self._list.setMinimumHeight(380)
        self._list.itemSelectionChanged.connect(self._on_select)
        layout.addWidget(self._list, stretch=1)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self._new_btn = make_ghost_button("New")
        self._import_btn = make_ghost_button("Import")
        self._export_btn = make_ghost_button("Export")
        self._delete_btn = make_danger_button("Delete")
        for btn in (self._new_btn, self._import_btn, self._export_btn, self._delete_btn):
            actions.addWidget(btn)
        layout.addLayout(actions)

        self._new_btn.clicked.connect(self._on_new)
        self._import_btn.clicked.connect(self._on_import)
        self._export_btn.clicked.connect(self._on_export)
        self._delete_btn.clicked.connect(self._on_delete)
        return container

    def _build_editor_panel(self) -> QWidget:
        editor = CardWidget()
        editor.layout_v.addWidget(make_section_header("Edit Profile"))

        scroll_body = QWidget()
        scroll_layout = QVBoxLayout(scroll_body)
        scroll_layout.setContentsMargins(2, 2, 12, 2)
        scroll_layout.setSpacing(16)

        scroll_layout.addWidget(self._build_identity_group())
        scroll_layout.addWidget(self._build_login_group())
        scroll_layout.addWidget(self._build_address_group())
        scroll_layout.addWidget(self._build_bank_group())
        scroll_layout.addWidget(self._build_attendees_group())
        scroll_layout.addWidget(self._build_custom_group())
        scroll_layout.addWidget(self._build_notes_group())
        scroll_layout.addStretch(1)

        editor.layout_v.addWidget(make_scroll_container(scroll_body), stretch=1)

        self._save_btn = make_primary_button("Save profile")
        self._save_btn.setMinimumHeight(38)
        self._save_btn.clicked.connect(self._on_save)
        editor.layout_v.addWidget(self._save_btn)
        return editor

    def _build_identity_group(self) -> QGroupBox:
        self._name = QLineEdit()
        self._full_name = QLineEdit()
        self._first_name = QLineEdit()
        self._last_name = QLineEdit()
        self._email = QLineEdit()
        self._phone = QLineEdit()
        self._dob = QLineEdit()
        self._dob.setPlaceholderText("YYYY-MM-DD")
        self._gender = QLineEdit()
        self._company = QLineEdit()

        group = QGroupBox("Identity")
        form = QFormLayout(group)
        configure_form(form)
        for label, widget in (
            ("Profile Name", self._name),
            ("Full Name", self._full_name),
            ("First Name", self._first_name),
            ("Last Name", self._last_name),
            ("Email", self._email),
            ("Phone", self._phone),
            ("Date of Birth", self._dob),
            ("Gender", self._gender),
            ("Company", self._company),
        ):
            make_form_row(form, label, widget)
        return group

    def _build_login_group(self) -> QGroupBox:
        self._login_email = QLineEdit()
        self._login_email.setPlaceholderText("Email used to sign in (e.g. tiket.com account)")
        self._login_password = QLineEdit()
        self._login_password.setEchoMode(QLineEdit.EchoMode.Password)
        self._login_password.setPlaceholderText("Account password")
        self._login_site = QLineEdit()
        self._login_site.setPlaceholderText("Optional: tiket.com")

        group = QGroupBox("Login (encrypted at rest)")
        form = QFormLayout(group)
        configure_form(form)
        for label, widget in (
            ("Login Email", self._login_email),
            ("Password", self._login_password),
            ("Site", self._login_site),
        ):
            make_form_row(form, label, widget)
        return group

    def _build_address_group(self) -> QGroupBox:
        self._addr1 = QLineEdit()
        self._addr2 = QLineEdit()
        self._city = QLineEdit()
        self._state = QLineEdit()
        self._postal = QLineEdit()
        self._country = QLineEdit()

        group = QGroupBox("Address")
        form = QFormLayout(group)
        configure_form(form)
        for label, widget in (
            ("Address Line 1", self._addr1),
            ("Address Line 2", self._addr2),
            ("City", self._city),
            ("State", self._state),
            ("Postal Code", self._postal),
            ("Country", self._country),
        ):
            make_form_row(form, label, widget)
        return group

    def _build_bank_group(self) -> QGroupBox:
        self._bank_holder = QLineEdit()
        self._bank_name = QLineEdit()
        self._bank_account = QLineEdit()
        self._bank_account.setEchoMode(QLineEdit.EchoMode.Password)
        self._bank_routing = QLineEdit()
        self._bank_routing.setEchoMode(QLineEdit.EchoMode.Password)
        self._bank_iban = QLineEdit()
        self._bank_iban.setEchoMode(QLineEdit.EchoMode.Password)
        self._bank_swift = QLineEdit()

        group = QGroupBox("Bank (encrypted at rest)")
        form = QFormLayout(group)
        configure_form(form)
        for label, widget in (
            ("Account Holder", self._bank_holder),
            ("Bank Name", self._bank_name),
            ("Account Number", self._bank_account),
            ("Routing Number", self._bank_routing),
            ("IBAN", self._bank_iban),
            ("SWIFT/BIC", self._bank_swift),
        ):
            make_form_row(form, label, widget)
        return group

    def _build_attendees_group(self) -> QGroupBox:
        group = QGroupBox("Attendees (one JSON object per line)")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(12, 24, 12, 12)
        layout.setSpacing(6)
        self._attendees = QPlainTextEdit()
        self._attendees.setPlaceholderText(
            'One JSON object per line, e.g. {"full_name": "Jane Doe", "email": "jane@example.com"}'
        )
        self._attendees.setMinimumHeight(120)
        layout.addWidget(self._attendees)
        return group

    def _build_custom_group(self) -> QGroupBox:
        group = QGroupBox("Custom fields")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(12, 24, 12, 12)
        layout.setSpacing(6)
        self._custom = QPlainTextEdit()
        self._custom.setPlaceholderText("key=value per line, e.g. company_id=98765")
        self._custom.setMinimumHeight(110)
        layout.addWidget(self._custom)
        return group

    def _build_notes_group(self) -> QGroupBox:
        group = QGroupBox("Notes")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(12, 24, 12, 12)
        layout.setSpacing(6)
        self._notes = QPlainTextEdit()
        self._notes.setMinimumHeight(80)
        layout.addWidget(self._notes)
        return group

    async def load_initial_data(self) -> None:
        """Triggered by the main window once async services are running."""

        profiles = await self._profile_manager.list_profiles()
        self.profiles_loaded.emit(profiles)

    def _on_profiles_changed(self, profiles: list[Profile]) -> None:
        self.profiles_loaded.emit(list(profiles))

    def _render_profiles(self, profiles: list[Profile]) -> None:
        self._suppress_select = True
        keep_id = self._current.id if self._current else None
        self._list.clear()
        for profile in profiles:
            item = QListWidgetItem(profile.name)
            item.setData(Qt.ItemDataRole.UserRole, profile.id)
            self._list.addItem(item)
        restored = False
        if keep_id is not None:
            for index in range(self._list.count()):
                item = self._list.item(index)
                if item.data(Qt.ItemDataRole.UserRole) == keep_id:
                    self._list.setCurrentItem(item)
                    restored = True
                    break
        self._suppress_select = False
        if restored:
            return
        if not profiles:
            self._current = None
            self._render_form(None)
            return
        self._list.setCurrentRow(0)

    def _on_select(self) -> None:
        if self._suppress_select:
            return
        items = self._list.selectedItems()
        if not items:
            return
        profile_id = items[0].data(Qt.ItemDataRole.UserRole)
        if self._current is not None and self._current.id == profile_id:
            return
        run_async(self._select_profile(profile_id))

    async def _select_profile(self, profile_id: str) -> None:
        try:
            profile = await self._profile_manager.get(profile_id)
        except ProfileNotFoundError:
            return
        self._current = profile
        self._render_form(profile)
        self.profile_selected.emit(profile.id)

    def _render_form(self, profile: Profile | None) -> None:
        if profile is None:
            for widget in self._all_text_widgets():
                widget.clear()
            self._attendees.clear()
            self._custom.clear()
            self._notes.clear()
            return
        self._name.setText(profile.name)
        self._full_name.setText(profile.full_name)
        self._first_name.setText(profile.first_name)
        self._last_name.setText(profile.last_name)
        self._email.setText(str(profile.email) if profile.email else "")
        self._phone.setText(profile.phone)
        self._dob.setText(profile.date_of_birth or "")
        self._gender.setText(profile.gender or "")
        self._company.setText(profile.company)
        self._addr1.setText(profile.address.line1)
        self._addr2.setText(profile.address.line2)
        self._city.setText(profile.address.city)
        self._state.setText(profile.address.state)
        self._postal.setText(profile.address.postal_code)
        self._country.setText(profile.address.country)
        self._bank_holder.setText(profile.bank.account_holder)
        self._bank_name.setText(profile.bank.bank_name)
        self._bank_account.setText(
            profile.bank.account_number.get_secret_value() if profile.bank.account_number else ""
        )
        self._bank_routing.setText(
            profile.bank.routing_number.get_secret_value() if profile.bank.routing_number else ""
        )
        self._bank_iban.setText(profile.bank.iban.get_secret_value() if profile.bank.iban else "")
        self._bank_swift.setText(profile.bank.swift)
        self._login_email.setText(profile.login.email)
        self._login_password.setText(profile.login.password_value() or "")
        self._login_site.setText(profile.login.site)
        self._notes.setPlainText(profile.notes)
        attendees_text = "\n".join(
            attendee.model_dump_json(exclude_none=True) for attendee in profile.attendees
        )
        self._attendees.setPlainText(attendees_text)
        custom_text = "\n".join(f"{k}={v}" for k, v in profile.custom_fields.items())
        self._custom.setPlainText(custom_text)

    def _all_text_widgets(self) -> list[QLineEdit]:
        return [
            self._name,
            self._full_name,
            self._first_name,
            self._last_name,
            self._email,
            self._phone,
            self._dob,
            self._gender,
            self._company,
            self._addr1,
            self._addr2,
            self._city,
            self._state,
            self._postal,
            self._country,
            self._bank_holder,
            self._bank_name,
            self._bank_account,
            self._bank_routing,
            self._bank_iban,
            self._bank_swift,
            self._login_email,
            self._login_password,
            self._login_site,
        ]

    def _on_new(self) -> None:
        self._current = Profile(name="New Profile")
        self._suppress_select = True
        self._list.clearSelection()
        self._suppress_select = False
        self._render_form(self._current)
        self._name.setFocus()
        self._name.selectAll()

    def _on_save(self) -> None:
        try:
            profile = self._build_profile_from_form()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid profile", str(exc))
            return
        run_async(self._save_profile(profile))

    async def _save_profile(self, profile: Profile) -> None:
        try:
            saved = await self._profile_manager.save(profile)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self._current = saved

    def _build_profile_from_form(self) -> Profile:
        name = self._name.text().strip()
        if not name:
            raise ValueError("Profile name is required")
        attendees: list[Attendee] = []
        for line in self._attendees.toPlainText().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                attendees.append(Attendee.model_validate_json(line))
            except Exception as exc:
                raise ValueError(f"Invalid attendee JSON: {exc}") from exc

        custom: dict[str, str] = {}
        for line in self._custom.toPlainText().splitlines():
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            custom[key.strip()] = value.strip()

        bank = BankInfo(
            account_holder=self._bank_holder.text().strip(),
            bank_name=self._bank_name.text().strip(),
            account_number=self._bank_account.text() or None,
            routing_number=self._bank_routing.text() or None,
            iban=self._bank_iban.text() or None,
            swift=self._bank_swift.text().strip(),
        )
        login = LoginCredentials(
            email=self._login_email.text().strip(),
            password=self._login_password.text() or None,
            site=self._login_site.text().strip(),
        )
        address = Address(
            line1=self._addr1.text().strip(),
            line2=self._addr2.text().strip(),
            city=self._city.text().strip(),
            state=self._state.text().strip(),
            postal_code=self._postal.text().strip(),
            country=self._country.text().strip(),
        )

        existing = self._current
        return Profile(
            id=existing.id if existing else generate_id(),
            name=name,
            full_name=self._full_name.text().strip(),
            first_name=self._first_name.text().strip(),
            last_name=self._last_name.text().strip(),
            email=self._email.text().strip() or None,
            phone=self._phone.text().strip(),
            date_of_birth=self._dob.text().strip() or None,
            gender=self._gender.text().strip() or None,
            company=self._company.text().strip(),
            notes=self._notes.toPlainText().strip(),
            address=address,
            bank=bank,
            login=login,
            attendees=attendees,
            custom_fields=custom,
        )

    def _on_import(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Import profile", str(Path.home()), "Profiles (*.json *.yaml *.yml)"
        )
        if not path_str:
            return
        run_async(self._import_async(Path(path_str)))

    async def _import_async(self, path: Path) -> None:
        try:
            await self._profile_manager.import_file(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Import failed", str(exc))

    def _on_export(self) -> None:
        if self._current is None:
            QMessageBox.information(self, "Export profile", "Select a profile first.")
            return
        path_str, selected = QFileDialog.getSaveFileName(
            self,
            "Export profile",
            str(Path.home() / f"{self._current.name}.json"),
            "JSON (*.json);;YAML (*.yaml)",
        )
        if not path_str:
            return
        path = Path(path_str)
        if path.suffix.lower() in {".yaml", ".yml"} or "YAML" in selected:
            run_async(self._profile_manager.export_yaml(self._current.id, path))
        else:
            run_async(self._profile_manager.export_json(self._current.id, path))

    def _on_delete(self) -> None:
        if self._current is None:
            return
        confirm = QMessageBox.question(
            self,
            "Delete profile",
            f"Delete profile '{self._current.name}'? This cannot be undone.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        run_async(self._delete_async(self._current.id))

    async def _delete_async(self, profile_id: str) -> None:
        try:
            await self._profile_manager.delete(profile_id)
            self._current = None
            self._render_form(None)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Delete failed", str(exc))
