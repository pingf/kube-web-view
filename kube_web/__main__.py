import aiohttp_jinja2
import jinja2
import csv
import pykube
import logging
import yaml

from pykube import ObjectDoesNotExist
from pykube.objects import APIObject, NamespacedAPIObject, Namespace

from pathlib import Path

from aiohttp import web

from kube_web import __version__

logging.basicConfig(level=logging.DEBUG)

logger = logging.getLogger(__name__)


try:
    api = pykube.HTTPClient(pykube.KubeConfig.from_service_account())
    CLUSTER_NAME = "local"
except:
    kubeconfig = pykube.KubeConfig.from_file()
    api = pykube.HTTPClient(kubeconfig)
    CLUSTER_NAME = kubeconfig.current_context


def discover_api_resources(api):
    core_version = "v1"
    r = api.get(version=core_version)
    r.raise_for_status()
    for resource in r.json()["resources"]:
        # ignore subresources like pods/proxy
        if (
            "/" not in resource["name"]
            and "get" in resource["verbs"]
            and "list" in resource["verbs"]
        ):
            yield resource["namespaced"], core_version, resource

    r = api.get(version="/apis")
    r.raise_for_status()
    for group in r.json()["groups"]:
        pref_version = group["preferredVersion"]["groupVersion"]
        yielded = set()
        non_preferred = []
        for version in group["versions"]:
            group_version = version["groupVersion"]
            logger.debug(f"Collecting resources for {group_version}..")
            r2 = api.get(version=group_version)
            r2.raise_for_status()
            for resource in r2.json()["resources"]:
                if (
                    "/" not in resource["name"]
                    and "get" in resource["verbs"]
                    and "list" in resource["verbs"]
                ):
                    if group_version == pref_version:
                        yield resource["namespaced"], group_version, resource
                        yielded.add(resource["name"])
                    else:
                        non_preferred.append(
                            (resource["namespaced"], group_version, resource)
                        )
        for namespaced, group_version, resource in non_preferred:
            if resource["name"] not in yielded:
                yield namespaced, group_version, resource


def cluster_object_factory(kind: str, name: str, api_version: str):
    # https://github.com/kelproject/pykube/blob/master/pykube/objects.py#L138
    return type(
        kind, (APIObject,), {"version": api_version, "endpoint": name, "kind": kind}
    )


def namespaced_object_factory(kind: str, name: str, api_version: str):
    # https://github.com/kelproject/pykube/blob/master/pykube/objects.py#L138
    return type(
        kind,
        (NamespacedAPIObject,),
        {"version": api_version, "endpoint": name, "kind": kind},
    )


def get_namespaced_resource_types(api):
    for namespaced, api_version, resource in discover_api_resources(api):
        if namespaced:
            clazz = namespaced_object_factory(
                resource["kind"], resource["name"], api_version
            )
            yield clazz
        else:
            clazz = cluster_object_factory(
                resource["kind"], resource["name"], api_version
            )
            yield clazz


cluster_resource_types = []
namespaced_resource_types = []

for clazz in get_namespaced_resource_types(api):
    if issubclass(clazz, NamespacedAPIObject):
        namespaced_resource_types.append(clazz)
    else:
        cluster_resource_types.append(clazz)

routes = web.RouteTableDef()


def context():
    def decorator(func):
        async def func_wrapper(request):
            ctx = await func(request)
            if isinstance(ctx, dict):
                namespaces = list(Namespace.objects(api).filter())
                ctx["namespaces"] = namespaces
                ctx["rel_url"] = request.rel_url
            return ctx

        return func_wrapper

    return decorator


@routes.get("/")
async def get_index(request):
    raise web.HTTPFound(location="/clusters")


@routes.get("/clusters")
@aiohttp_jinja2.template("clusters.html")
async def get_clusters(request):
    return {"clusters": [CLUSTER_NAME]}


@routes.get("/clusters/{cluster}")
@aiohttp_jinja2.template("cluster.html")
@context()
async def get_cluster(request):
    cluster = request.match_info["cluster"]
    namespaces = list(Namespace.objects(api).filter())
    return {
        "cluster": cluster,
        "namespace": None,
        "namespaces": namespaces,
        "resource_types": cluster_resource_types,
    }


@routes.get("/clusters/{cluster}/{plural}")
@aiohttp_jinja2.template("resource-list.html")
@context()
async def get_cluster_resource_list(request):
    cluster = request.match_info["cluster"]
    plural = request.match_info["plural"]
    params = request.rel_url.query
    clazz = None
    for c in cluster_resource_types:
        if c.endpoint == plural:
            clazz = c
            break
    if not clazz:
        return web.Response(status=404, text="Resource type not found")
    table = clazz.objects(api).as_table()
    if params.get("download") == "tsv":
        return await download_tsv(request, table)
    return {"cluster": cluster, "namespace": None, "plural": plural, "tables": [table]}


@routes.get("/clusters/{cluster}/{plural}/{name}")
@aiohttp_jinja2.template("resource-view.html")
@context()
async def get_cluster_resource_view(request):
    cluster = request.match_info["cluster"]
    plural = request.match_info["plural"]
    name = request.match_info["name"]
    view = request.rel_url.query.get("view")
    clazz = None
    for c in cluster_resource_types:
        if c.endpoint == plural:
            clazz = c
            break
    if not clazz:
        return web.Response(status=404, text="Resource type not found")
    try:
        resource = clazz.objects(api).get(name=name)
    except ObjectDoesNotExist:
        return web.Response(status=404, text="Resource does not exist")
    if resource.kind == "Namespace":
        namespace = resource.name
    else:
        namespace = None
    return {
        "cluster": cluster,
        "namespace": namespace,
        "plural": plural,
        "resource": resource,
        "view": view,
    }


class ResponseWriter:
    def __init__(self, response):
        self.response = response
        self.data = ""

    def write(self, data):
        self.data += data

    async def flush(self):
        await self.response.write(self.data.encode("utf-8"))
        self.data = ""


async def as_tsv(table, fd) -> str:
    writer = csv.writer(fd, delimiter="\t", lineterminator="\n")
    writer.writerow([col["name"] for col in table.columns])
    for row in table.rows:
        writer.writerow(row["cells"])
        await fd.flush()


async def download_tsv(request, table):
    response = web.StreamResponse()
    response.content_type = "text/tab-separated-values"
    path = request.rel_url.path
    filename = path.strip("/").replace("/", "_")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}.tsv"'
    await response.prepare(request)
    await as_tsv(table, ResponseWriter(response))
    return response


@routes.get("/clusters/{cluster}/namespaces/{namespace}/{plural}")
@aiohttp_jinja2.template("resource-list.html")
@context()
async def get_namespaced_resource_list(request):
    cluster = request.match_info["cluster"]
    namespace = request.match_info["namespace"]
    plural = request.match_info["plural"]
    params = request.rel_url.query
    tables = []
    for _type in plural.split(","):
        clazz = None
        for c in namespaced_resource_types:
            if c.endpoint == _type:
                clazz = c
                break
        if not clazz:
            return web.Response(status=404, text="Resource type not found")
        query = clazz.objects(api).filter(namespace=namespace)
        if "selector" in params:
            query = query.filter(selector=params["selector"])

        table = query.as_table()
        tables.append(table)
    if params.get("download") == "tsv":
        return await download_tsv(request, tables[0])
    return {
        "cluster": cluster,
        "namespace": namespace,
        "plural": plural,
        "tables": tables,
    }


@routes.get("/clusters/{cluster}/namespaces/{namespace}/{plural}/{name}")
@aiohttp_jinja2.template("resource-view.html")
@context()
async def get_namespaced_resource_view(request):
    cluster = request.match_info["cluster"]
    namespace = request.match_info["namespace"]
    plural = request.match_info["plural"]
    name = request.match_info["name"]
    view = request.rel_url.query.get("view")
    clazz = None
    for c in namespaced_resource_types:
        if c.endpoint == plural:
            clazz = c
            break
    if not clazz:
        return web.Response(status=404, text="Resource type not found")
    resource = clazz.objects(api).filter(namespace=namespace).get(name=name)
    return {
        "cluster": cluster,
        "namespace": namespace,
        "plural": plural,
        "resource": resource,
        "view": view,
    }


@routes.get("/health")
async def get_health(request):
    return web.Response(text="OK")


def filter_yaml(value):
    return yaml.dump(value, default_flow_style=False)


def filter_highlight(value):
    from pygments import highlight
    from pygments.lexers import get_lexer_by_name
    from pygments.formatters import HtmlFormatter

    return highlight(value, get_lexer_by_name("yaml"), HtmlFormatter())


app = web.Application()
aiohttp_jinja2.setup(
    app, loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates"))
)
env = aiohttp_jinja2.get_env(app)
env.filters.update(yaml=filter_yaml, highlight=filter_highlight)
env.globals["version"] = __version__

app.add_routes(routes)
app.router.add_static("/assets", Path(__file__).parent / "templates" / "assets")

web.run_app(app)
