"""QA (stream 3): language switch and i18n coverage, plus automatable checks of the minimal UI
rules. Spec: docs/build-prompt.md section 6 (FR/EN, remembered per user, default French) and
section 7 (minimal UI, hover guidance with aria-describedby, one primary button per form,
labels, plain tables, no images, emoji, inline colours, badges).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import lexwatch.web
from lexwatch.db import get_db
from lexwatch.i18n import catalog
from lexwatch.models import RUN_KINDS, RUN_STATUSES, TRACKED_BILL_FIELDS, Run, User
from lexwatch.web.app import create_app
from tests.helpers import csrf_token, make_test_settings
from tests.web.factories import (
    dev_user,
    make_bill,
    make_event,
    make_match,
    make_profile,
    other_user,
)

CSS_PATH = Path(lexwatch.web.__file__).parent / "static" / "style.css"
AREAS = "app|nav|lang|footer|bills|bill|rules|admin|auth|error|digest|email"
RAW_KEY_RE = re.compile(rf"(?<![\w@./-])(?:{AREAS})\.[a-z_]+(?:\.[a-z_]+)*(?![\w@/-])")
FILE_RE = re.compile(r"\.(csv|css|js|json|html)$")
PLACEHOLDER_RE = re.compile(r"\{[a-z_]+\}")
EMOJI_RE = re.compile("[\U0001f000-\U0001faff☀-➿⬀-⯿️‍]")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source"}
CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


# --- a tiny DOM -----------------------------------------------------------------------------


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    parent: Node | None
    children: list[Node] = field(default_factory=list)
    text: list[str] = field(default_factory=list)

    def ancestors(self) -> Iterator[Node]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def iter(self) -> Iterator[Node]:
        yield self
        for child in self.children:
            yield from child.iter()

    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def all_text(self) -> str:
        return " ".join(["".join(self.text)] + [c.all_text() for c in self.children])


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: v or "" for k, v in attrs}, self.current)
        self.current.children.append(node)
        if tag not in VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.current.children.append(Node(tag, {k: v or "" for k, v in attrs}, self.current))

    def handle_endtag(self, tag):
        node = self.current
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.current = node.parent

    def handle_data(self, data):
        self.current.text.append(data)


def dom(html: str) -> Node:
    builder = _Builder()
    builder.feed(html)
    return builder.root


def visible_strings(root: Node) -> list[str]:
    """Text nodes (outside script/style) plus user-facing attribute values."""
    out: list[str] = []
    for node in root.iter():
        if node.tag in ("script", "style"):
            continue
        out.extend(t for t in node.text if t.strip())
        for attr in ("title", "aria-label", "placeholder", "alt"):
            if node.attrs.get(attr):
                out.append(node.attrs[attr])
        if node.tag == "input" and node.attrs.get("type") in ("submit", "button"):
            out.append(node.attrs.get("value", ""))
    return out


# --- data and screens -----------------------------------------------------------------------


@pytest.fixture
def screens(db: Session, admin_client: TestClient) -> dict[str, str]:
    admin = dev_user(db, "admin")
    admin.previous_visit_at = datetime.now(UTC) - timedelta(days=2)
    owner = other_user(db, "ui-owner")
    profile = make_profile(db, owner, name="Profil UI", rules=("regle a", "regle b"))
    pending = make_profile(db, admin, name="Profil en attente", threshold=0.42)
    pending.rescore_status = "pending"
    bill = make_bill(db, "8410", title="Loi sur la pêche", status="adopted")
    for f in TRACKED_BILL_FIELDS:
        make_event(db, bill, f, "ancien", "nouveau")
    make_match(db, profile, bill, 0.8)
    for i in range(51):
        make_bill(db, str(30000 + i), last_changed_at=datetime.now(UTC))
    start = datetime.now(UTC) - timedelta(hours=2)
    for kind in RUN_KINDS:
        for status in RUN_STATUSES:
            db.add(
                Run(
                    kind=kind,
                    status=status,
                    started_at=start,
                    finished_at=start + timedelta(seconds=30),
                    error_message="erreur" if status in ("failed", "partial") else None,
                    details={
                        "sources": {
                            "chd": {"status": "failed", "error": "x"},
                            "datapublic-112": {"status": "ok", "records": 3},
                            "datapublic-121": {"status": "skipped"},
                        },
                        "billing": {"unit": "input_tokens"},
                    },
                    billed_units=5,
                )
            )
    db.flush()
    return {
        "home": "/",
        "home_profile_all": f"/?profile={profile.id}&all=1",
        "home_search_empty": "/?q=zzzzzz",
        "home_page2": "/?page=2&all=1",
        "bill": "/bills/8410",
        "rules": "/rules",
        "rules_new": "/rules/new",
        "rules_edit": f"/rules/{profile.id}",
        "rules_edit_pending": f"/rules/{pending.id}",
        "rules_delete": f"/rules/{profile.id}/delete",
        "admin": "/admin",
        "not_found": "/no-such-page",
    }


def set_lang(client: TestClient, lang: str) -> None:
    response = client.post(
        "/lang",
        data={"csrf_token": csrf_token(client), "lang": lang, "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def get_html(client: TestClient, path: str) -> str:
    response = client.get(path, headers={"Accept": "text/html"})
    assert response.status_code in (200, 404), (path, response.status_code)
    return response.text


def invalid_form_html(client: TestClient) -> str:
    response = client.post(
        "/rules/new",
        data={
            "csrf_token": csrf_token(client),
            "name": "",
            "threshold": "7",
            "recipients": "pas-un-email",
            "rule_id": [""],
            "rule_text": ["x" * 1001],
            "action": "save",
        },
    )
    assert response.status_code == 422
    return response.text


def all_pages(client: TestClient, screens: dict[str, str]) -> dict[str, str]:
    pages = {name: get_html(client, path) for name, path in screens.items()}
    pages["rules_invalid"] = invalid_form_html(client)
    pages["forbidden"] = client.get(
        "/rules/999999", headers={"Accept": "text/html"}
    ).text  # 404 page
    pages["preview"] = client.post(
        "/rules/preview",
        data={"csrf_token": csrf_token(client), "rule_text": ["blanchiment"], "threshold": ""},
    ).text
    return pages


# --- language -------------------------------------------------------------------------------


@pytest.fixture
def anon_client(db: Session) -> Iterator[TestClient]:
    application = create_app(make_test_settings(dev_auth=False))

    def _get_db() -> Iterator[Session]:
        yield db

    application.dependency_overrides[get_db] = _get_db
    with TestClient(application) as c:
        yield c


def _html_lang(html: str) -> str:
    return re.search(r'<html lang="([a-z]+)"', html).group(1)


def test_raw_key_detector_detects_keys():
    assert RAW_KEY_RE.search("bills.col.title")
    assert RAW_KEY_RE.search("Voir rules.form.save ici")
    assert not RAW_KEY_RE.search("admin@lexwatch.local")
    assert not RAW_KEY_RE.search("https://www.chd.lu/fr/dossier/8410")
    strings = visible_strings(dom('<p title="nav.x">a</p><button>error.generic</button>'))
    assert strings == ["a", "nav.x", "error.generic"]


def test_default_language_is_french(admin_client):
    assert _html_lang(admin_client.get("/").text) == "fr"
    assert catalog("fr")["nav.rules"] in admin_client.get("/").text


def test_anonymous_default_is_french_and_switch_is_kept_in_session(anon_client, app):
    page = anon_client.get("/nowhere", headers={"Accept": "text/html"}).text
    assert _html_lang(page) == "fr"
    token = CSRF_RE.search(page).group(1)
    response = anon_client.post(
        "/lang",
        data={"csrf_token": token, "lang": "en", "next": "/nowhere"},
        headers={"Accept": "text/html"},
    )
    assert _html_lang(response.text) == "en"
    assert _html_lang(anon_client.get("/x", headers={"Accept": "text/html"}).text) == "en"
    # Another anonymous visitor is not affected.
    with TestClient(anon_client.app) as other:
        assert _html_lang(other.get("/x", headers={"Accept": "text/html"}).text) == "fr"


def test_language_is_stored_on_user_and_survives_new_session(app, db):
    with TestClient(app) as first:
        first.get("/login?as=user")
        set_lang(first, "en")
    user = db.get(User, dev_user(db, "user").id)
    db.refresh(user)
    assert user.language == "en"
    with TestClient(app) as second:
        second.get("/login?as=user")
        assert _html_lang(second.get("/").text) == "en"
        set_lang(second, "fr")
    db.refresh(user)
    assert user.language == "fr"


def test_user_language_overrides_session_choice(app, db):
    user = dev_user(db, "user")
    user.language = "en"
    db.flush()
    with TestClient(app) as c:
        c.get("/login?as=user")
        assert _html_lang(c.get("/").text) == "en"


@pytest.mark.parametrize("value", ["de", "", "EN", "fr-FR", "<script>", "../../etc"])
def test_unknown_language_is_normalized(admin_client, db, value):
    response = admin_client.post(
        "/lang",
        data={"csrf_token": csrf_token(admin_client), "lang": value, "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code in (303, 422)
    db.expire_all()
    assert dev_user(db, "admin").language in ("fr", "en")
    assert _html_lang(admin_client.get("/").text) in ("fr", "en")


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/",
        "//evil.example/",
        "http:evil.example",
        "javascript:alert(1)",
        "evil.example",
        "\\\\evil.example",
    ],
)
def test_lang_next_is_not_an_open_redirect(admin_client, target):
    response = admin_client.post(
        "/lang",
        data={"csrf_token": csrf_token(admin_client), "lang": "en", "next": target},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/") and not location.startswith("//")
    assert "evil" not in location and "javascript" not in location


@pytest.mark.parametrize("target", ["/\\evil.example", "/\\/evil.example", "/\t/evil.example"])
def test_lang_next_backslash_is_not_an_open_redirect(admin_client, target):
    response = admin_client.post(
        "/lang",
        data={"csrf_token": csrf_token(admin_client), "lang": "en", "next": target},
        follow_redirects=False,
    )
    location = response.headers["location"]
    # Browsers treat a raw "/\\" like "//"; a percent-encoded backslash is a plain path.
    assert not location.startswith(("//", "/\\")), location
    assert not re.match(r"^/[\t\n\r]*[/\\]", location), location


def test_lang_next_keeps_local_path_and_query(admin_client):
    response = admin_client.post(
        "/lang",
        data={"csrf_token": csrf_token(admin_client), "lang": "en", "next": "/?q=loi&all=1"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/?q=loi&all=1"


# --- i18n coverage on every screen ----------------------------------------------------------


@pytest.mark.parametrize("lang", ["fr", "en"])
def test_every_screen_renders_without_raw_keys(admin_client, screens, lang):
    set_lang(admin_client, lang)
    for name, html in all_pages(admin_client, screens).items():
        root = dom(html)
        if name != "preview":
            assert _html_lang(html) == lang, name
        for text in visible_strings(root):
            keys = [k for k in RAW_KEY_RE.findall(text) if not FILE_RE.search(k)]
            assert not keys, (name, text)
            assert not PLACEHOLDER_RE.search(text), (name, text)


def test_screens_differ_between_languages(admin_client, screens):
    set_lang(admin_client, "fr")
    fr = all_pages(admin_client, screens)
    set_lang(admin_client, "en")
    en = all_pages(admin_client, screens)
    for name in fr:
        if name == "preview":
            continue
        assert fr[name] != en[name], name
    assert catalog("en")["nav.rules"] in en["home"]
    assert catalog("fr")["nav.rules"] in fr["home"]


def test_every_tracked_field_status_kind_and_run_status_has_a_label():
    for lang in ("fr", "en"):
        keys = catalog(lang)
        for f in TRACKED_BILL_FIELDS:
            assert f"bill.field.{f}" in keys, (lang, f)
        for kind in RUN_KINDS:
            assert f"admin.runs.kind.{kind}" in keys
        for status in RUN_STATUSES:
            assert f"admin.runs.status.{status}" in keys
        for bill_type in ("projet", "proposition"):
            assert f"bill.type.{bill_type}" in keys


def test_history_field_names_are_translated(admin_client, screens):
    for lang in ("fr", "en"):
        set_lang(admin_client, lang)
        page = admin_client.get("/bills/8410").text
        for f in TRACKED_BILL_FIELDS:
            assert f">{f}<" not in page, (lang, f)


# --- minimal UI rules (section 7) -----------------------------------------------------------


@pytest.fixture
def pages_both(admin_client, screens) -> dict[str, str]:
    out: dict[str, str] = {}
    for lang in ("fr", "en"):
        set_lang(admin_client, lang)
        for name, html in all_pages(admin_client, screens).items():
            out[f"{lang}:{name}"] = html
    return out


def test_no_images_icons_or_emoji(pages_both):
    for name, html in pages_both.items():
        root = dom(html)
        for node in root.iter():
            assert node.tag not in ("img", "svg", "picture", "canvas", "video", "i"), (
                name,
                node.tag,
            )
        for text in visible_strings(root):
            assert not EMOJI_RE.search(text), (name, text)


def test_no_inline_styles_or_style_blocks(pages_both):
    for name, html in pages_both.items():
        root = dom(html)
        for node in root.iter():
            assert "style" not in node.attrs, (name, node.tag, node.attrs)
            assert node.tag != "style", name
            assert "color" not in node.attrs and "bgcolor" not in node.attrs, name


def test_no_badges_chips_or_stat_cards(pages_both):
    banned = re.compile(r"\b(badge|chip|pill|tag|card|stat|label-|icon|avatar)\b")
    for name, html in pages_both.items():
        for node in dom(html).iter():
            for cls in node.classes():
                assert not banned.search(cls), (name, cls)


def _forms(root: Node) -> list[Node]:
    return [n for n in root.iter() if n.tag == "form"]


def _primary_buttons(form: Node) -> list[Node]:
    return [n for n in form.iter() if n.tag in ("button", "input") and "primary" in n.classes()]


def test_no_form_has_more_than_one_primary_button(pages_both):
    for name, html in pages_both.items():
        for form in _forms(dom(html)):
            assert len(_primary_buttons(form)) <= 1, (name, form.attrs)


def _main(root: Node) -> Node:
    return next(n for n in root.iter() if n.tag == "main")


@pytest.mark.parametrize("screen", ["rules_new", "rules_edit", "rules_delete", "rules_invalid"])
def test_content_forms_have_exactly_one_primary_button(pages_both, screen):
    for lang in ("fr", "en"):
        for form in _forms(_main(dom(pages_both[f"{lang}:{screen}"]))):
            assert len(_primary_buttons(form)) == 1, (lang, screen, form.attrs)


def test_bills_search_button_is_secondary(pages_both):
    """Filtering is live (htmx); the Search button is a plain fallback, not a primary action."""
    for lang in ("fr", "en"):
        for form in _forms(_main(dom(pages_both[f"{lang}:home"]))):
            assert _primary_buttons(form) == [], (lang, form.attrs)


def test_admin_page_has_exactly_one_primary_button(pages_both):
    """ "Run now" is the primary action; "Rescore all" is a quieter secondary button."""
    forms = _forms(_main(dom(pages_both["fr:admin"])))
    assert sum(len(_primary_buttons(form)) for form in forms) == 1
    primary = next(f for f in forms if _primary_buttons(f))
    assert primary.attrs.get("action") == "/admin/run"


def test_every_field_has_a_label(pages_both):
    for name, html in pages_both.items():
        root = dom(html)
        label_for = {n.attrs.get("for") for n in root.iter() if n.tag == "label"}
        for node in root.iter():
            if node.tag not in ("input", "select", "textarea"):
                continue
            if node.attrs.get("type") in ("hidden", "submit", "button"):
                continue
            labelled = (
                node.attrs.get("id") in label_for
                or node.attrs.get("aria-label")
                or node.attrs.get("aria-labelledby")
                or any(a.tag == "label" for a in node.ancestors())
            )
            assert labelled, (name, node.tag, node.attrs)


def test_aria_describedby_targets_exist_and_ids_are_unique(pages_both):
    for name, html in pages_both.items():
        root = dom(html)
        ids = [n.attrs["id"] for n in root.iter() if "id" in n.attrs]
        assert len(ids) == len(set(ids)), (name, sorted({i for i in ids if ids.count(i) > 1}))
        for node in root.iter():
            for ref in node.attrs.get("aria-describedby", "").split():
                assert ref in ids, (name, node.tag, ref)


def test_hover_guidance_in_content_is_connected_to_aria_describedby(pages_both):
    for name, html in pages_both.items():
        root = dom(html)
        mains = [n for n in root.iter() if n.tag == "main"]
        scope = mains[0] if mains else root  # fragments (preview) have no <main>
        for node in scope.iter():
            if "title" not in node.attrs:
                continue
            connected = "aria-describedby" in node.attrs or any(
                "aria-describedby" in a.attrs for a in node.ancestors()
            )
            assert connected, (name, node.tag, node.attrs)


def test_hover_elements_are_keyboard_reachable(pages_both):
    focusable = {"a", "button", "input", "select", "textarea", "summary"}
    for name, html in pages_both.items():
        for node in dom(html).iter():
            if "hint" in node.classes():
                assert node.tag in focusable or node.attrs.get("tabindex") == "0", (
                    name,
                    node.attrs,
                )


def test_table_headers_have_scope(pages_both):
    for name, html in pages_both.items():
        for node in dom(html).iter():
            if node.tag == "th":
                assert node.attrs.get("scope") in ("col", "row", "colgroup", "rowgroup"), name


def test_no_helper_captions_next_to_fields(pages_both):
    """Guidance is on hover only: no visible <small>, .help or .hint text under fields."""
    for name, html in pages_both.items():
        for node in dom(html).iter():
            assert node.tag != "small", name
            assert not ({"help", "helper", "caption", "info"} & node.classes()), (name, node.attrs)


def test_top_bar_has_name_sections_language_and_user_menu(pages_both):
    root = dom(pages_both["fr:home"])
    header = next(n for n in root.iter() if n.tag == "header")
    text = header.all_text()
    assert catalog("fr")["app.name"] in text
    for key in ("nav.bills", "nav.rules", "nav.admin", "nav.logout"):
        assert catalog("fr")[key] in text, key
    assert "FR" in text and "EN" in text


def test_footer_has_chd_attribution(pages_both):
    for name, html in pages_both.items():
        if name.endswith(":preview"):
            continue
        assert 'href="https://www.chd.lu"' in html, name


# --- stylesheet -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def css() -> str:
    return CSS_PATH.read_text("utf-8")


def test_css_has_no_decoration(css):
    lowered = css.lower()
    for banned in ("gradient", "box-shadow", "text-shadow", "@keyframes", "animation"):
        assert banned not in lowered, banned
    assert "@import" not in lowered and "url(" not in lowered


def test_css_uses_a_system_font_stack(css):
    assert "system-ui" in css
    assert "@font-face" not in css


def test_css_has_at_most_three_type_sizes(css):
    sizes = set(re.findall(r"font-size:\s*([^;}]+)", css))
    literal = {s.strip() for s in sizes if not s.strip().startswith(("var(", "inherit"))}
    variables = set(re.findall(r"--size-[a-z]+:\s*([^;]+);", css))
    assert len(literal | variables) <= 3, literal | variables


def _hex_colours(css: str) -> set[str]:
    out = set()
    for raw in re.findall(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b", css):
        if len(raw) == 3:
            raw = "".join(c * 2 for c in raw)
        out.add(raw.lower())
    return out


def test_css_has_one_accent_and_one_error_hue(css):
    chromatic = set()
    for hex_ in _hex_colours(css):
        r, g, b = (int(hex_[i : i + 2], 16) for i in (0, 2, 4))
        if max(r, g, b) - min(r, g, b) > 24:  # not a grey
            chromatic.add(hex_)
    hues = set()
    for hex_ in chromatic:
        r, g, b = (int(hex_[i : i + 2], 16) for i in (0, 2, 4))
        hues.add("red" if r > max(g, b) else "blue")
    # The accent may have a darker hover shade; still one accent hue plus one error hue.
    assert len(hues) <= 2, chromatic
    assert re.search(r"rgb\(|rgba\(|hsl\(", css) is None or len(hues) <= 2


def test_css_focus_is_visible(css):
    assert ":focus-visible" in css or ":focus" in css
    assert "outline: none" not in css.replace("main:focus { outline: none; }", "")


def test_only_subtle_htmx_transition(css):
    transitions = re.findall(r"([^{}]+)\{[^}]*transition:", css)
    for selector in transitions:
        assert "htmx" in selector, selector


def test_every_page_title_uses_the_same_separator():
    templates = Path(__file__).resolve().parents[2] / "src/lexwatch/web/templates"
    for path in templates.rglob("*.html"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "{% block title %}" in line and "t('app.name')" in line and "{% endblock" in line:
                inner = line.split("{% block title %}", 1)[1]
                if inner.startswith("{{ t('app.name') }}"):
                    continue
                assert " · {{ t('app.name') }}" in inner, (path.name, line)


def test_hints_have_a_css_tooltip_for_keyboard_focus(pages_both):
    css = (Path(__file__).resolve().parents[2] / "src/lexwatch/web/static/style.css").read_text(
        encoding="utf-8"
    )
    assert ".hint[data-tip]:focus::after" in css and ".hint[data-tip]:hover::after" in css
    found = 0
    for name, html in pages_both.items():
        for node in dom(html).iter():
            if "hint" in node.classes():
                found += 1
                attrs = node.attrs
                assert attrs.get("data-tip") and attrs.get("title") == attrs.get("data-tip"), name
                assert attrs.get("tabindex") == "0" and attrs.get("aria-describedby"), name
    assert found
