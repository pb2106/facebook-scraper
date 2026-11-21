import html as html_lib
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from . import exceptions, utils
from .constants import DEFAULT_PROFILE_TIMELINE_REFETCH_DOC_ID, FB_W3_BASE_URL
from .fb_types import Post

logger = logging.getLogger(__name__)

_SCRIPT_JSON_RE = re.compile(
    r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>', re.DOTALL
)
_JS_URL_RE = re.compile(
    r'https://static\.xx\.fbcdn\.net/rsrc\.php/[^"\'\\]+\.js'
)
_FEED_CONTAINER_KEYS = ("timeline_list_feed_units", "group_feed")


def is_comet_html(text: str) -> bool:
    """Return True if the response text is a Comet/Relay SSR HTML page."""
    if not text:
        return False
    return 'type="application/json"' in text and (
        "data-sjs" in text or "ScheduledServerJS" in text or "timeline_list_feed_units" in text
    )


def is_graphql_ndjson(text: str) -> bool:
    """Return True if the response text looks like a GraphQL JSON/NDJSON payload."""
    if not text:
        return False
    stripped = text.lstrip()
    if stripped.startswith("for (;;);"):
        stripped = stripped[len("for (;;);") :].lstrip()
    return stripped.startswith('{"data":') or stripped.startswith('{"errors":')


def extract_sjs_scripts(html_text: str) -> List[Any]:
    """Parse all application/json script tags in a Comet SSR HTML document."""
    payloads = []
    for match in _SCRIPT_JSON_RE.finditer(html_text):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            payloads.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return payloads


def _search_regex(pattern: str, text: str) -> Optional[str]:
    match = re.search(pattern, text)
    return match.group(1) if match else None


def extract_relay_context(
    html_text: str, url: Optional[str] = None, cookies=None
) -> Dict[str, Any]:
    """Extract GraphQL tokens, preloader query variables, and JS bundle URLs from SSR HTML."""
    lsd = _search_regex(r'"LSD",\[\],\{"token":"([^"]+)"', html_text)
    if not lsd:
        lsd = _search_regex(r'"lsd":"([^"]+)"', html_text)

    fb_dtsg = _search_regex(r'"DTSGInitialData",\[\],\{"token":"([^"]+)"', html_text)
    jazoest = _search_regex(r'jazoest=(\d+)', html_text)
    rev = _search_regex(r'"__spin_r":(\d+)', html_text) or _search_regex(r'"rev":(\d+)', html_text)
    spin_b = _search_regex(r'"__spin_b":"([^"]+)"', html_text) or "trunk"
    spin_t = _search_regex(r'"__spin_t":(\d+)', html_text)
    hsi = _search_regex(r'"hsi":"([^"]+)"', html_text)
    hs = _search_regex(r'"haste_session":"([^"]+)"', html_text)

    user_id = "0"
    if cookies is not None:
        c_user = cookies.get("c_user") if hasattr(cookies, "get") else None
        if c_user:
            user_id = str(c_user)

    js_urls = list(dict.fromkeys(_JS_URL_RE.findall(html_text)))

    query_name = None
    query_id = None
    query_vars = None
    for payload in extract_sjs_scripts(html_text):
        found = _find_feed_preloader(payload)
        if found:
            query_name, query_id, query_vars = found
            break

    return {
        "lsd": lsd,
        "fb_dtsg": fb_dtsg,
        "jazoest": jazoest,
        "rev": rev,
        "spin_b": spin_b,
        "spin_t": spin_t,
        "hsi": hsi,
        "hs": hs,
        "user_id": user_id,
        "js_urls": js_urls,
        "query_name": query_name,
        "query_id": query_id,
        "query_vars": query_vars or {},
        "referer": url or FB_W3_BASE_URL,
    }


def _find_feed_preloader(obj: Any) -> Optional[Tuple[str, str, Dict[str, Any]]]:
    target_queries = {
        "ProfileCometTimelineFeedQuery",
        "GroupsCometFeedRegularStoriesPaginationQuery",
        "SearchCometInterestsDeepDivePostsQuery",
    }
    if isinstance(obj, dict):
        qname = obj.get("queryName")
        if qname in target_queries and isinstance(obj.get("variables"), dict):
            return qname, str(obj.get("queryID") or ""), obj["variables"]
        for v in obj.values():
            res = _find_feed_preloader(v)
            if res:
                return res
    elif isinstance(obj, list):
        for item in obj:
            res = _find_feed_preloader(item)
            if res:
                return res
    return None


def resolve_doc_id(
    js_urls: List[str],
    operation_name: str,
    request_fn,
    cache: Dict[str, str],
    fallback: Optional[str] = None,
) -> Optional[str]:
    """Resolve and cache the Relay doc_id for an operation from the page's JS bundles."""
    if operation_name in cache:
        return cache[operation_name]

    marker = f'__d("{operation_name}_facebookRelayOperation"'
    op_regex = re.compile(
        rf'__d\("{re.escape(operation_name)}_facebookRelayOperation",\[[^\]]*\],\(function\([^)]*\)\{{[^}}]*a\.exports="(\d+)"'
    )
    for js_url in js_urls:
        try:
            resp = request_fn(js_url)
            text = getattr(resp, "text", "")
            if marker not in text:
                continue
            match = op_regex.search(text)
            if match:
                doc_id = match.group(1)
                cache[operation_name] = doc_id
                logger.debug("Resolved doc_id for %s: %s", operation_name, doc_id)
                return doc_id
        except Exception as ex:
            logger.debug("Failed inspecting JS bundle %s: %r", js_url, ex)

    if fallback:
        cache[operation_name] = fallback
    return fallback


def _collect_stories_and_page_info(
    payloads: List[Any],
) -> Tuple[List[Dict[str, Any]], Optional[str], bool]:
    """Extract top-level feed/permalink Story nodes and pagination cursor from Relay payloads."""
    stories: List[Dict[str, Any]] = []
    seen_ids = set()
    next_cursor: Optional[str] = None
    has_next_page = False

    def add_story(story: Any):
        if not isinstance(story, dict) or story.get("__typename") != "Story":
            return
        if "comet_sections" not in story and "message" not in story and "post_id" not in story:
            return
        sid = str(story.get("post_id") or story.get("id") or "")
        if sid and sid in seen_ids:
            return
        if sid:
            seen_ids.add(sid)
        stories.append(story)

    def walk(obj: Any):
        nonlocal next_cursor, has_next_page
        if isinstance(obj, dict):
            # Feed containers (timeline or group)
            for container_key in _FEED_CONTAINER_KEYS:
                container = obj.get(container_key)
                if isinstance(container, dict):
                    for edge in container.get("edges") or []:
                        if isinstance(edge, dict):
                            add_story(edge.get("node"))
                    pi = container.get("page_info")
                    if isinstance(pi, dict) and pi.get("has_next_page") and pi.get("end_cursor"):
                        next_cursor = pi["end_cursor"]
                        has_next_page = True

            # Hashtag / topic deep dive feed
            tdd = obj.get("topic_deep_dive")
            if isinstance(tdd, dict):
                rs = tdd.get("rendering_strategies")
                if isinstance(rs, dict):
                    for edge in rs.get("edges") or []:
                        if not isinstance(edge, dict):
                            continue
                        story = (
                            (edge.get("rendering_strategy") or {})
                            .get("explore_view_model", {})
                            .get("story")
                        )
                        add_story(story)
                    pi = rs.get("page_info")
                    if isinstance(pi, dict) and pi.get("has_next_page") and pi.get("end_cursor"):
                        next_cursor = pi["end_cursor"]
                        has_next_page = True

            # Deferred page_info payload
            label = obj.get("label")
            if isinstance(label, str) and label.endswith("$page_info"):
                data = obj.get("data")
                if isinstance(data, dict):
                    pi = data.get("page_info")
                    if isinstance(pi, dict):
                        if pi.get("has_next_page") and pi.get("end_cursor"):
                            next_cursor = pi["end_cursor"]
                            has_next_page = True
                        elif pi.get("has_next_page") is False:
                            has_next_page = False

            # Single-post permalink (node_v2 or direct node)
            for node_key in ("node_v2", "node"):
                candidate = obj.get(node_key)
                if (
                    isinstance(candidate, dict)
                    and candidate.get("__typename") == "Story"
                    and "comet_sections" in candidate
                ):
                    add_story(candidate)

            for k, v in obj.items():
                if k in ("attached_story", "comet_sections"):
                    continue
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    for payload in payloads:
        walk(payload)

    if not stories:
        video_story = _extract_video_permalink_story(payloads)
        if video_story:
            stories.append(video_story)

    return stories, next_cursor, has_next_page


def _extract_video_permalink_story(payloads: List[Any]) -> Optional[Dict[str, Any]]:
    """Synthesize a Story dict from video/Watch permalink SSR payloads (/videos/<id>/)."""
    video_media: Optional[Dict[str, Any]] = None
    feedback_data: Optional[Dict[str, Any]] = None
    creation_story_meta: Optional[Dict[str, Any]] = None
    video_id: Optional[str] = None
    message_obj: Optional[Dict[str, Any]] = None
    owner_id: Optional[str] = None
    owner_name: Optional[str] = None
    owner_url: Optional[str] = None

    def collect_owner(own: Any):
        nonlocal owner_id, owner_name, owner_url
        if not isinstance(own, dict):
            return
        as_page = own.get("owner_as_page") or {}
        owner_id = owner_id or as_page.get("id") or (str(own["id"]) if own.get("id") else None)
        owner_name = owner_name or as_page.get("name") or own.get("name")
        owner_url = owner_url or own.get("url") or own.get("profile_url")

    def walk(obj: Any):
        nonlocal video_media, feedback_data, creation_story_meta, video_id, message_obj
        if isinstance(obj, dict):
            if "videoDeliveryLegacyFields" in obj and obj.get("__typename") == "Video":
                video_media = obj
                video_id = video_id or (str(obj["id"]) if obj.get("id") else None)
                collect_owner(obj.get("owner"))
            if (
                isinstance(obj.get("feedback"), dict)
                and "video_view_count_renderer" in obj["feedback"]
            ):
                feedback_data = obj["feedback"]
                video_id = video_id or (str(obj["id"]) if obj.get("id") else None)
                collect_owner(obj.get("owner"))
                cs_inner = obj.get("creation_story") or {}
                if isinstance(cs_inner.get("message"), dict):
                    message_obj = cs_inner["message"]
            if (
                obj.get("__typename") == "Video"
                and isinstance(obj.get("creation_story"), dict)
                and "comet_sections" in obj["creation_story"]
            ):
                creation_story_meta = obj["creation_story"]
                video_id = video_id or (str(obj["id"]) if obj.get("id") else None)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    for payload in payloads:
        walk(payload)

    if not video_media and not feedback_data:
        return None

    cs_sections = (creation_story_meta or {}).get("comet_sections") or {}
    if not message_obj:
        message_obj = ((cs_sections.get("message") or {}).get("story") or {}).get("message")

    creation_time = (video_media or {}).get("publish_time")
    permalink_url = (video_media or {}).get("permalink_url") or (video_media or {}).get("url")
    actors = []
    for meta in cs_sections.get("metadata") or []:
        st = (meta or {}).get("story") or {}
        creation_time = creation_time or st.get("creation_time")
        permalink_url = permalink_url or st.get("url")
        for act in st.get("actors") or []:
            collect_owner(act)

    if owner_id or owner_name or owner_url:
        actors = [{"id": owner_id, "name": owner_name, "url": owner_url}]

    story: Dict[str, Any] = {
        "__typename": "Story",
        "post_id": video_id,
        "creation_time": creation_time,
        "permalink_url": permalink_url,
        "actors": actors,
        "message": message_obj,
        "attachments": (
            [
                {
                    "media": {"__typename": "Video", "id": video_id},
                    "styles": {
                        "__typename": "StoryAttachmentVideoStyleRenderer",
                        "attachment": {"media": video_media},
                    },
                }
            ]
            if video_media
            else []
        ),
        "comet_sections": {
            "feedback": feedback_data or {},
        },
    }
    return story



def parse_relay_html(html_text: str) -> Tuple[List[Dict[str, Any]], Optional[str], bool]:
    """Parse stories and pagination info from a Comet SSR HTML page."""
    payloads = extract_sjs_scripts(html_text)
    return _collect_stories_and_page_info(payloads)


def _deep_merge(target: Any, source: Any):
    if isinstance(target, dict) and isinstance(source, dict):
        for k, v in source.items():
            if k in target and isinstance(target[k], dict) and isinstance(v, dict):
                _deep_merge(target[k], v)
            else:
                target[k] = v


def _apply_relay_patch(root: Dict[str, Any], path: List[Any], data: Any):
    curr: Any = root
    for segment in path[:-1]:
        if isinstance(curr, dict):
            if segment not in curr or curr[segment] is None:
                curr[segment] = {}
            curr = curr[segment]
        elif isinstance(curr, list) and isinstance(segment, int):
            while len(curr) <= segment:
                curr.append({})
            curr = curr[segment]
        else:
            return

    last = path[-1]
    if isinstance(curr, dict):
        if last in curr and isinstance(curr[last], dict) and isinstance(data, dict):
            _deep_merge(curr[last], data)
        else:
            curr[last] = data
    elif isinstance(curr, list) and isinstance(last, int):
        while len(curr) <= last:
            curr.append(None)
        if isinstance(curr[last], dict) and isinstance(data, dict):
            _deep_merge(curr[last], data)
        else:
            curr[last] = data


def parse_graphql_ndjson(text: str) -> Tuple[List[Dict[str, Any]], Optional[str], bool]:
    """Parse a streamed GraphQL NDJSON response, applying @defer and @stream patches."""
    stripped = text.lstrip()
    if stripped.startswith("for (;;);"):
        stripped = stripped[len("for (;;);") :].lstrip()

    chunks = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            chunks.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not chunks:
        return [], None, False

    errors = chunks[0].get("errors") or []
    if errors and not (chunks[0].get("data") or {}).get("node"):
        msg = "; ".join(e.get("message", "GraphQL error") for e in errors)
        codes = {e.get("code") for e in errors}
        if 1675004 in codes:
            logger.warning("Facebook GraphQL rate limit / blocked request: %s", msg)
        else:
            logger.warning("Facebook GraphQL error: %s", msg)
        return [], None, False

    root_data = chunks[0].get("data")
    if not isinstance(root_data, dict):
        return [], None, False

    for chunk in chunks[1:]:
        path = chunk.get("path")
        data = chunk.get("data")
        if isinstance(path, list) and path and data is not None:
            _apply_relay_patch(root_data, path, data)
        elif isinstance(chunk.get("data"), dict):
            # Fallback if a chunk carries top-level page_info without path
            if "page_info" in chunk["data"]:
                _collect_into = chunk["data"]
                root_data.setdefault("_extra_chunks", []).append(_collect_into)

    stories, next_cursor, has_next_page = _collect_stories_and_page_info([root_data])

    # Also check raw chunks for page_info in case path didn't land inside _FEED_CONTAINER_KEYS
    if not next_cursor:
        for chunk in chunks[1:]:
            d = chunk.get("data")
            if isinstance(d, dict) and isinstance(d.get("page_info"), dict):
                pi = d["page_info"]
                if pi.get("has_next_page") and pi.get("end_cursor"):
                    next_cursor = pi["end_cursor"]
                    has_next_page = True
                    break

    return stories, next_cursor, has_next_page


def build_graphql_pagination_request(
    relay_context: Dict[str, Any],
    cursor: str,
    doc_id: str,
    posts_per_page: Optional[int] = None,
    operation_name: str = "ProfileCometTimelineFeedRefetchQuery",
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Build form data and headers for a Comet GraphQL pagination POST."""
    variables = dict(relay_context.get("query_vars") or {})
    if "userID" in variables and "id" not in variables:
        variables["id"] = variables.pop("userID")
    variables["cursor"] = cursor
    variables["count"] = posts_per_page or 3

    user_id = relay_context.get("user_id") or "0"
    data = {
        "av": user_id,
        "__user": user_id,
        "__a": "1",
        "dpr": "1",
        "__ccg": "EXCELLENT",
        "__comet_req": "15",
        "fb_api_caller_class": "RelayModern",
        "fb_api_req_friendly_name": operation_name,
        "variables": json.dumps(variables),
        "server_timestamps": "true",
        "doc_id": doc_id,
    }
    optional_fields = {
        "__hs": "hs",
        "__rev": "rev",
        "__hsi": "hsi",
        "lsd": "lsd",
        "jazoest": "jazoest",
        "__spin_r": "rev",
        "__spin_b": "spin_b",
        "__spin_t": "spin_t",
        "fb_dtsg": "fb_dtsg",
    }
    for form_key, ctx_key in optional_fields.items():
        val = relay_context.get(ctx_key)
        if val:
            data[form_key] = str(val)

    headers = {
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": FB_W3_BASE_URL.rstrip("/"),
        "Referer": relay_context.get("referer") or FB_W3_BASE_URL,
        "X-FB-Friendly-Name": operation_name,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    if relay_context.get("lsd"):
        headers["X-FB-LSD"] = relay_context["lsd"]

    return data, headers


def _empty_post() -> Post:
    return {
        "post_id": None,
        "text": None,
        "post_text": None,
        "shared_text": None,
        "original_text": None,
        "time": None,
        "timestamp": None,
        "image": None,
        "image_lowquality": None,
        "images": None,
        "images_description": None,
        "images_lowquality": None,
        "images_lowquality_description": None,
        "video": None,
        "video_duration_seconds": None,
        "video_height": None,
        "video_id": None,
        "video_quality": None,
        "video_size_MB": None,
        "video_thumbnail": None,
        "video_watches": None,
        "video_width": None,
        "likes": None,
        "comments": None,
        "shares": None,
        "post_url": None,
        "link": None,
        "links": None,
        "user_id": None,
        "username": None,
        "user_url": None,
        "source": None,
        "is_live": False,
        "factcheck": None,
        "shared_post_id": None,
        "shared_time": None,
        "shared_user_id": None,
        "shared_username": None,
        "shared_user_url": None,
        "shared_post_url": None,
        "available": None,
        "comments_full": None,
        "reactors": None,
        "w3_fb_url": None,
        "reactions": None,
        "reaction_count": None,
        "with": None,
        "page_id": None,
        "sharers": None,
    }


def _clean_fb_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    return utils.filter_query_params(
        url, blacklist=["__cft__[0]", "__tn__", "__xts__[0]", "fbclid"]
    )


def _unwrap_linkshim(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.netloc in ("l.facebook.com", "lm.facebook.com") and parsed.path == "/l.php":
        u_vals = parse_qs(parsed.query).get("u")
        if u_vals:
            return unquote(u_vals[0])
    return url


def _find_first(obj: Any, predicate) -> Any:
    if predicate(obj):
        return obj
    if isinstance(obj, dict):
        for v in obj.values():
            res = _find_first(v, predicate)
            if res is not None:
                return res
    elif isinstance(obj, list):
        for item in obj:
            res = _find_first(item, predicate)
            if res is not None:
                return res
    return None


def _extract_story_message(story: Dict[str, Any]) -> Tuple[Optional[str], List[Dict[str, str]]]:
    """Extract post message text and inline external links from a Comet Story."""
    cs = story.get("comet_sections") or {}
    content = cs.get("content") or {}
    content_story = content.get("story") or {}
    content_cs = content_story.get("comet_sections") or {}

    msg_obj = (
        (content_cs.get("message") or {}).get("story", {}).get("message")
        or (content_cs.get("message_container") or {}).get("story", {}).get("message")
        or content_story.get("message")
        or (cs.get("message") or {}).get("story", {}).get("message")
        or story.get("message")
    )
    if not isinstance(msg_obj, dict):
        # Fallback: locate first dict with "message": {"text": ...} inside content
        found = _find_first(
            content,
            lambda x: isinstance(x, dict)
            and isinstance(x.get("message"), dict)
            and "text" in x["message"],
        )
        msg_obj = found["message"] if found else None

    if not isinstance(msg_obj, dict):
        return None, []

    text = msg_obj.get("text")
    links = []
    for r in msg_obj.get("ranges") or []:
        entity = r.get("entity") or {}
        ext_url = (
            entity.get("external_url")
            or (entity.get("web_link") or {}).get("url")
            or _unwrap_linkshim(entity.get("url"))
        )
        if ext_url:
            links.append({"link": ext_url, "text": ext_url})
    return text, links


def _extract_story_actor(story: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract (user_id, username, user_url) from a Comet Story."""
    cs = story.get("comet_sections") or {}
    ctx = (cs.get("context_layout") or {}).get("story") or {}
    ctx_cs = ctx.get("comet_sections") or {}

    actor_candidates = []
    for candidate in (
        ((ctx_cs.get("actor_photo") or {}).get("story") or {}).get("actors"),
        ((ctx_cs.get("title") or {}).get("story") or {}).get("actors"),
        ctx.get("actors"),
        story.get("actors"),
    ):
        if isinstance(candidate, list) and candidate:
            actor_candidates.extend(candidate)

    # Also check owning_profile in feedback
    owning = (
        ((cs.get("feedback") or {}).get("story") or {})
        .get("feedback_context", {})
        .get("feedback_target_with_context", {})
    )
    if not owning:
        owning = (story.get("feedback") or {}).get("owning_profile")
    else:
        owning = owning.get("owning_profile")
    if isinstance(owning, dict):
        actor_candidates.append(owning)

    user_id = None
    username = None
    user_url = None
    for actor in actor_candidates:
        if not isinstance(actor, dict):
            continue
        user_id = user_id or (str(actor["id"]) if actor.get("id") else None)
        username = username or actor.get("name")
        user_url = user_url or actor.get("url") or actor.get("profile_url")
        if user_id and username and user_url:
            break

    return user_id, username, user_url


def _extract_story_timestamp(story: Dict[str, Any]) -> Optional[int]:
    if story.get("creation_time"):
        return int(story["creation_time"])
    cs = story.get("comet_sections") or {}
    ts_story = (cs.get("timestamp") or {}).get("story") or {}
    if ts_story.get("creation_time"):
        return int(ts_story["creation_time"])
    ctx_cs = ((cs.get("context_layout") or {}).get("story") or {}).get("comet_sections") or {}
    for meta in ctx_cs.get("metadata") or []:
        if isinstance(meta, dict):
            st = meta.get("story") or {}
            if st.get("creation_time"):
                return int(st["creation_time"])
    return None


def _extract_story_url(story: Dict[str, Any], post_id: Optional[str]) -> Optional[str]:
    cs = story.get("comet_sections") or {}
    ctx_cs = ((cs.get("context_layout") or {}).get("story") or {}).get("comet_sections") or {}
    candidates = [story.get("permalink_url"), story.get("url")]
    ts_story = (cs.get("timestamp") or {}).get("story") or {}
    candidates.append(ts_story.get("url"))
    for meta in ctx_cs.get("metadata") or []:
        if isinstance(meta, dict):
            candidates.append((meta.get("story") or {}).get("url"))
    for url in candidates:
        if url:
            return _clean_fb_url(url)
    if post_id:
        return f"{FB_W3_BASE_URL}{post_id}"
    return None


def _extract_attachments(story: Dict[str, Any]) -> Dict[str, Any]:
    images: List[str] = []
    images_lq: List[str] = []
    images_desc: List[str] = []
    images_lq_desc: List[str] = []
    image_ids: List[str] = []
    links: List[Dict[str, str]] = []
    video_info: Dict[str, Any] = {}

    cs = story.get("comet_sections") or {}
    attachments = story.get("attachments") or (
        ((cs.get("content") or {}).get("story") or {}).get("attachments")
    ) or []

    def add_photo(media: Dict[str, Any]):
        if not isinstance(media, dict):
            return
        mid = media.get("id")
        lq = (media.get("image") or {}).get("uri") or (media.get("photo_image") or {}).get("uri")
        hq = (media.get("viewer_image") or {}).get("uri") or (media.get("photo_image") or {}).get(
            "uri"
        ) or lq
        desc = media.get("accessibility_caption")
        if hq:
            images.append(hq)
            images_desc.append(desc)
        if lq:
            images_lq.append(lq)
            images_lq_desc.append(desc)
        if mid:
            image_ids.append(str(mid))

    for att in attachments:
        if not isinstance(att, dict):
            continue
        styles = att.get("styles") or {}
        st_name = styles.get("__typename")
        attachment = styles.get("attachment") or {}
        media = attachment.get("media") or att.get("media") or {}

        if st_name == "StoryAttachmentAlbumStyleRenderer":
            nodes = (attachment.get("all_subattachments") or {}).get("nodes") or []
            for sub in nodes:
                if isinstance(sub, dict):
                    add_photo(sub.get("media") or {})

        elif st_name in (
            "StoryAttachmentPhotoStyleRenderer",
            "StoryAttachmentCoverPhotoStyleRenderer",
        ) or media.get("__typename") == "Photo":
            add_photo(media)

        elif st_name == "StoryAttachmentVideoStyleRenderer" or media.get("__typename") == "Video":
            delivery = media.get("videoDeliveryLegacyFields") or {}
            video_url = (
                delivery.get("browser_native_hd_url")
                or delivery.get("browser_native_sd_url")
                or media.get("playable_url_quality_hd")
                or media.get("playable_url")
            )
            if video_url:
                video_info["video"] = video_url
            if media.get("id"):
                video_info["video_id"] = str(media["id"])
            thumb = (media.get("thumbnailImage") or {}).get("uri") or (
                (media.get("preferred_thumbnail") or {}).get("image") or {}
            ).get("uri")
            if thumb:
                video_info["video_thumbnail"] = thumb
            dur_ms = media.get("playable_duration_in_ms")
            if dur_ms is not None:
                video_info["video_duration_seconds"] = dur_ms / 1000.0
            if media.get("width"):
                video_info["video_width"] = media["width"]
            if media.get("height"):
                video_info["video_height"] = media["height"]
            if media.get("is_live_streaming") or media.get("is_live_broadcast"):
                video_info["is_live"] = True

        elif st_name == "StoryAttachmentShareStyleRenderer":
            link_att = (attachment.get("story_attachment_link_renderer") or {}).get(
                "attachment"
            ) or {}
            ext_url = (link_att.get("web_link") or {}).get("url") or _unwrap_linkshim(
                link_att.get("url")
            )
            title_text = (attachment.get("title_with_entities") or {}).get("text")
            if ext_url:
                links.append({"link": ext_url, "text": title_text or ext_url})
            share_img = (media.get("large_share_image") or {}).get("uri")
            if share_img and not images:
                images.append(share_img)
                images_lq.append(share_img)
                images_desc.append(None)
                images_lq_desc.append(None)

    return {
        "images": images,
        "images_lowquality": images_lq,
        "images_description": images_desc,
        "images_lowquality_description": images_lq_desc,
        "image_ids": image_ids,
        "links": links,
        "video_info": video_info,
    }


def _extract_feedback(story: Dict[str, Any]) -> Dict[str, Any]:
    cs = story.get("comet_sections") or {}
    fb_section = cs.get("feedback") or {}

    summary = _find_first(
        fb_section,
        lambda x: isinstance(x, dict) and "comet_ufi_summary_and_actions_renderer" in x,
    )
    fb_data = {}
    if summary:
        fb_data = (summary.get("comet_ufi_summary_and_actions_renderer") or {}).get(
            "feedback"
        ) or {}
    if not fb_data:
        fb_data = _find_first(
            fb_section,
            lambda x: isinstance(x, dict)
            and ("top_reactions" in x or "reaction_count" in x or "share_count" in x),
        ) or {}

    reactions: Dict[str, int] = {}
    top_reactions = fb_data.get("top_reactions") or {}
    for edge in top_reactions.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        node = edge.get("node") or {}
        name = node.get("localized_name") or node.get("reaction_type")
        count = edge.get("reaction_count")
        if name and count is not None:
            reactions[name.lower()] = int(count)

    reaction_count = (fb_data.get("reaction_count") or {}).get("count")
    if reaction_count is None and reactions:
        reaction_count = sum(reactions.values())

    likes = reactions.get("like")
    if likes is None:
        likes = reaction_count

    share_count = (fb_data.get("share_count") or {}).get("count")

    comment_count = (
        (fb_data.get("comment_rendering_instance") or {}).get("comments") or {}
    ).get("total_count")
    if comment_count is None:
        cri = _find_first(
            fb_section,
            lambda x: isinstance(x, dict)
            and isinstance(x.get("comment_rendering_instance"), dict),
        )
        if cri:
            comment_count = (
                (cri["comment_rendering_instance"].get("comments") or {}).get("total_count")
            )

    video_watches = None
    vvc_holder = _find_first(
        fb_section,
        lambda x: isinstance(x, dict)
        and (
            "video_view_count_renderer" in x
            or x.get("video_view_count") is not None
            or x.get("video_post_view_count") is not None
        ),
    )
    if vvc_holder:
        vvc_fb = (
            (vvc_holder.get("video_view_count_renderer") or {}).get("feedback") or vvc_holder
        )
        for key in ("video_view_count", "video_post_view_count", "play_count"):
            if vvc_fb.get(key) is not None:
                video_watches = int(vvc_fb[key])
                break

    return {
        "likes": likes,
        "reactions": reactions or None,
        "reaction_count": reaction_count,
        "shares": share_count,
        "comments": comment_count,
        "video_watches": video_watches,
        "feedback_section": fb_section,
    }


def _extract_comments_from_story(
    fb_section: Dict[str, Any], limit: Any = True
) -> Optional[List[Dict[str, Any]]]:
    clr = _find_first(
        fb_section,
        lambda x: isinstance(x, dict) and "comment_list_renderer" in x,
    )
    if not clr:
        return None
    edges = (
        (clr.get("comment_list_renderer") or {})
        .get("feedback", {})
        .get("comment_rendering_instance_for_feed_location", {})
        .get("comments", {})
        .get("edges")
    )
    if not isinstance(edges, list):
        return None

    max_count = limit if isinstance(limit, int) and not isinstance(limit, bool) else None
    results = []
    for edge in edges:
        if max_count is not None and len(results) >= max_count:
            break
        node = (edge or {}).get("node") or {}
        if not node:
            continue
        author = node.get("author") or {}
        body = node.get("body") or node.get("preferred_body") or {}
        created_ts = node.get("created_time")
        comment_url = (node.get("feedback") or {}).get("url")
        if not comment_url:
            for link in node.get("comment_action_links") or []:
                comment_url = (link.get("comment") or {}).get("url")
                if comment_url:
                    break
        results.append(
            {
                "comment_id": node.get("legacy_fbid") or node.get("id"),
                "comment_url": _clean_fb_url(comment_url),
                "commenter_id": author.get("id"),
                "commenter_url": _clean_fb_url(author.get("url")),
                "commenter_name": author.get("name"),
                "commenter_meta": None,
                "comment_text": body.get("text"),
                "comment_time": datetime.fromtimestamp(created_ts) if created_ts else None,
                "comment_image": None,
                "comment_reactors": [],
                "comment_reactions": None,
                "comment_reaction_count": None,
                "replies": [],
            }
        )
    return results


def extract_relay_post(
    story: Dict[str, Any], options: Optional[Dict[str, Any]] = None, request_fn=None
) -> Post:
    """Convert a Comet/Relay Story dict into a Post dictionary matching PostExtractor output."""
    options = options or {}
    post = _empty_post()

    post_id = story.get("post_id")
    if post_id is not None:
        post_id = str(post_id)
    post["post_id"] = post_id

    ts = _extract_story_timestamp(story)
    if ts is not None:
        post["timestamp"] = ts
        post["time"] = datetime.fromtimestamp(ts)

    post_url = _extract_story_url(story, post_id)
    post["post_url"] = post_url
    post["w3_fb_url"] = post_url
    post["available"] = True

    user_id, username, user_url = _extract_story_actor(story)
    post["user_id"] = user_id
    post["username"] = username
    post["user_url"] = user_url
    post["page_id"] = user_id

    post_text, msg_links = _extract_story_message(story)
    post["post_text"] = post_text

    shared_text = None
    attached = story.get("attached_story")
    if isinstance(attached, dict):
        shared_text, _ = _extract_story_message(attached)
        post["shared_text"] = shared_text
        shared_ts = _extract_story_timestamp(attached)
        post["shared_time"] = datetime.fromtimestamp(shared_ts) if shared_ts else None
        s_uid, s_uname, s_uurl = _extract_story_actor(attached)
        post["shared_user_id"] = s_uid
        post["shared_username"] = s_uname
        post["shared_user_url"] = s_uurl
        shared_url = _extract_story_url(attached, None)
        shared_pid = str(attached["post_id"]) if attached.get("post_id") else None
        if not shared_pid and shared_url:
            m_pid = re.search(r'/(\d+)/?(?:\?|$)', shared_url)
            if m_pid:
                shared_pid = m_pid.group(1)
        post["shared_post_id"] = shared_pid
        post["shared_post_url"] = shared_url or _extract_story_url(attached, shared_pid)
    else:
        post["shared_text"] = ""

    if post_text and shared_text:
        post["text"] = f"{post_text}\n\n{shared_text}"
    else:
        post["text"] = post_text or shared_text

    att_info = _extract_attachments(story)
    images = att_info["images"]
    images_lq = att_info["images_lowquality"]
    post["images"] = images
    post["images_lowquality"] = images_lq
    post["images_description"] = att_info["images_description"]
    post["images_lowquality_description"] = att_info["images_lowquality_description"]
    post["image_ids"] = att_info["image_ids"]
    post["image"] = images[0] if images else None
    post["image_lowquality"] = images_lq[0] if images_lq else None
    post["image_id"] = att_info["image_ids"][0] if att_info["image_ids"] else None

    all_links = att_info["links"] + [l for l in msg_links if l not in att_info["links"]]
    post["links"] = all_links or None
    post["link"] = all_links[0]["link"] if all_links else None

    if att_info["video_info"]:
        post.update(att_info["video_info"])

    fb_info = _extract_feedback(story)
    post["likes"] = fb_info["likes"]
    post["comments"] = fb_info["comments"]
    post["shares"] = fb_info["shares"]
    post["reactions"] = fb_info["reactions"]
    post["reaction_count"] = fb_info["reaction_count"]
    if fb_info.get("video_watches") is not None:
        post["video_watches"] = fb_info["video_watches"]
    post["fetched_time"] = datetime.now()

    if options.get("comments"):
        post["comments_full"] = _extract_comments_from_story(
            fb_info["feedback_section"], limit=options["comments"]
        )

    # Wrap JSON in an Element so write_posts_to_csv / dump_location work seamlessly
    try:
        escaped = html_lib.escape(json.dumps(story, ensure_ascii=False))
        post["source"] = utils.make_html_element(
            f'<script type="application/json">{escaped}</script>', url=FB_W3_BASE_URL
        )
    except Exception:
        post["source"] = None

    return post


def extract_relay_profile(html_text: str) -> Dict[str, Any]:
    """Extract profile/page metadata from a Comet SSR HTML page."""
    result: Dict[str, Any] = {}
    payloads = extract_sjs_scripts(html_text)

    stories, _, _ = _collect_stories_and_page_info(payloads)
    if stories:
        top_post = extract_relay_post(stories[0])
        top_post.pop("source", None)
        result["top_post"] = top_post

    def walk(obj: Any):
        if isinstance(obj, dict):
            hdr = obj.get("profile_header_renderer")
            if isinstance(hdr, dict):
                user = hdr.get("user") or {}
                if user.get("name"):
                    result.setdefault("Name", user["name"])
                    result.setdefault("name", user["name"])
                if user.get("id"):
                    result.setdefault("id", str(user["id"]))
                cover = ((user.get("cover_photo") or {}).get("photo") or {}).get("image") or {}
                if cover.get("uri"):
                    result.setdefault("cover_photo", cover["uri"])
                pic = (user.get("profile_picture_for_sticky_bar") or {}).get("uri")
                if pic:
                    result.setdefault("profile_picture", pic)
                for item in (user.get("profile_social_context") or {}).get("content") or []:
                    txt = (item.get("text") or {}).get("text") or ""
                    if "follower" in txt.lower():
                        try:
                            val = utils.convert_numeric_abbr(txt.split()[0])
                            result.setdefault("Follower_count", val)
                            result.setdefault("followers", val)
                        except Exception:
                            pass
                    elif "friend" in txt.lower():
                        try:
                            result.setdefault("Friend_count", utils.convert_numeric_abbr(txt.split()[0]))
                        except Exception:
                            pass

            if "profile_photo" in obj and isinstance(obj["profile_photo"], dict):
                pp = obj["profile_photo"]
                if (pp.get("image") or {}).get("uri"):
                    result["profile_picture"] = pp["image"]["uri"]

            if "profile_tile_sections" in obj and isinstance(obj["profile_tile_sections"], dict):
                for edge in obj["profile_tile_sections"].get("edges") or []:
                    for view_node in (
                        ((edge or {}).get("node") or {}).get("profile_tile_views") or {}
                    ).get("nodes") or []:
                        vsr = (view_node or {}).get("view_style_renderer") or {}
                        u = vsr.get("user") or {}
                        if u.get("id") and "id" not in result:
                            result["id"] = str(u["id"])
                        for item in (vsr.get("view") or {}).get("profile_tile_items", {}).get(
                            "nodes"
                        ) or []:
                            node = (item or {}).get("node") or {}
                            if node.get("__typename") == "ProfileStatus":
                                bio = (node.get("profile_status_text") or {}).get("text")
                                if bio:
                                    result.setdefault("about", bio)

            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    for payload in payloads:
        walk(payload)

    return result
