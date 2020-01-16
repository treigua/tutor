import codecs
from copy import deepcopy
import os

import jinja2
import pkg_resources

from . import exceptions
from . import fmt
from . import plugins
from . import utils
from .__about__ import __version__


TEMPLATES_ROOT = pkg_resources.resource_filename("tutor", "templates")
VERSION_FILENAME = "version"


class Renderer:
    INSTANCE = None

    @classmethod
    def instance(cls, config):
        if cls.INSTANCE is None or cls.INSTANCE.config != config:
            # Load template roots: these are required to be able to use
            # {% include .. %} directives
            template_roots = [TEMPLATES_ROOT]
            for plugin in plugins.iter_enabled(config):
                if plugin.templates_root:
                    template_roots.append(plugin.templates_root)

            cls.INSTANCE = cls(config, template_roots)
        return cls.INSTANCE

    @classmethod
    def reset(cls):
        cls.INSTANCE = None

    def __init__(self, config, template_roots):
        self.config = deepcopy(config)

        # Create environment
        environment = jinja2.Environment(
            loader=jinja2.FileSystemLoader(template_roots),
            undefined=jinja2.StrictUndefined,
        )
        environment.filters["random_string"] = utils.random_string
        environment.filters["common_domain"] = utils.common_domain
        environment.filters["list_if"] = utils.list_if
        environment.filters["reverse_host"] = utils.reverse_host
        environment.filters["walk_templates"] = self.walk_templates
        environment.globals["patch"] = self.patch
        environment.globals["TUTOR_VERSION"] = __version__
        self.environment = environment

    def iter_templates_in(self, *path):
        prefix = "/".join(path)
        for template in self.environment.loader.list_templates():
            if template.startswith(prefix) and is_part_of_env(template):
                yield template

    def walk_templates(self, subdir):
        """
        Iterate on the template files from `templates/<subdir>`.

        Yield:
            path: template path relative to the template root
        """
        yield from self.iter_templates_in(subdir + "/")

    def patch(self, name, separator="\n", suffix=""):
        """
        Render calls to {{ patch("...") }} in environment templates from plugin patches.
        """
        patches = []
        for plugin, patch in plugins.iter_patches(self.config, name):
            patch_template = self.environment.from_string(patch)
            try:
                patches.append(patch_template.render(**self.config))
            except jinja2.exceptions.UndefinedError as e:
                raise exceptions.TutorError(
                    "Missing configuration value: {} in patch '{}' from plugin {}".format(
                        e.args[0], name, plugin
                    )
                )
        rendered = separator.join(patches)
        if rendered:
            rendered += suffix
        return rendered

    def render_str(self, text):
        template = self.environment.from_string(text)
        return self.__render(template)

    def render_file(self, path):
        try:
            template = self.environment.get_template(path)
        except Exception:
            fmt.echo_error("Error loading template " + path)
            raise
        try:
            return self.__render(template)
        except (jinja2.exceptions.TemplateError, exceptions.TutorError):
            fmt.echo_error("Error rendering template " + path)
            raise
        except Exception:
            fmt.echo_error("Unknown error rendering template " + path)
            raise

    def __render(self, template):
        try:
            return template.render(**self.config)
        except jinja2.exceptions.UndefinedError as e:
            raise exceptions.TutorError(
                "Missing configuration value: {}".format(e.args[0])
            )


def save(root, config):
    """
    Save the full environment, including version information.
    """
    root_env = pathjoin(root)
    for prefix in [
        "android/",
        "apps/",
        "build/",
        "dev/",
        "k8s/",
        "local/",
        "webui/",
        VERSION_FILENAME,
        "kustomization.yml",
    ]:
        save_all_from(prefix, root_env, config)

    for plugin in plugins.iter_enabled(config):
        if plugin.templates_root:
            save_plugin_templates(plugin, root, config)

    fmt.echo_info("Environment generated in {}".format(base_dir(root)))


def save_plugin_templates(plugin, root, config):
    """
    Save plugin templates to plugins/<plugin name>/*.
    Only the "apps" and "build" subfolders are rendered.
    """
    plugins_root = pathjoin(root, "plugins")
    for subdir in ["apps", "build"]:
        subdir_path = os.path.join(plugin.name, subdir)
        save_all_from(subdir_path, plugins_root, config)


def save_all_from(prefix, root, config):
    """
    Render the templates that start with `prefix` and store them with the same
    hierarchy at `root`.
    """
    renderer = Renderer.instance(config)
    for template in renderer.iter_templates_in(prefix):
        rendered = renderer.render_file(template)
        dst = os.path.join(root, template)
        write_to(rendered, dst)


def write_to(content, path):
    utils.ensure_file_directory_exists(path)
    with open(path, "w") as of:
        of.write(content)


def render_file(config, *path):
    """
    Return the rendered contents of a template.
    """
    return Renderer.instance(config).render_file(os.path.join(*path))


def render_dict(config):
    """
    Render the values from the dict. This is useful for rendering the default
    values from config.yml.

    Args:
        config (dict)
    """
    rendered = {}
    for key, value in config.items():
        if isinstance(value, str):
            rendered[key] = render_str(config, value)
        else:
            rendered[key] = value
    for k, v in rendered.items():
        config[k] = v


def render_unknown(config, value):
    if isinstance(value, str):
        return render_str(config, value)
    return value


def render_str(config, text):
    """
    Args:
        text (str)
        config (dict)

    Return:
        substituted (str)
    """
    return Renderer.instance(config).render_str(text)


def check_is_up_to_date(root):
    if not is_up_to_date(root):
        message = (
            "The current environment stored at {} is not up-to-date: it is at "
            "v{} while the 'tutor' binary is at v{}. You should upgrade "
            "the environment by running:\n"
            "\n"
            "    tutor config save"
        )
        fmt.echo_alert(message.format(base_dir(root), version(root), __version__))


def is_up_to_date(root):
    """
    Check if the currently rendered version is equal to the current tutor version.
    """
    return version(root) == __version__


def version(root):
    """
    Return the current environment version. If the current environment has no version,
    return "0".
    """
    path = pathjoin(root, VERSION_FILENAME)
    if not os.path.exists(path):
        return "0"
    return open(path).read().strip()


def read(*path):
    """
    Read raw content of template located at `path`.
    """
    src = template_path(*path)
    with codecs.open(src, encoding="utf-8") as fi:
        return fi.read()


def is_part_of_env(path):
    """
    Determines whether a template should be rendered or not. Note that here we don't
    rely on the OS separator, as we are handling templates
    """
    parts = path.split("/")
    basename = parts[-1]
    is_excluded = False
    is_excluded = is_excluded or basename.startswith(".") or basename.endswith(".pyc")
    is_excluded = is_excluded or basename == "__pycache__"
    is_excluded = is_excluded or "partials" in parts
    return not is_excluded


def template_path(*path, templates_root=TEMPLATES_ROOT):
    """
    Return the template file's absolute path.
    """
    return os.path.join(templates_root, *path)


def data_path(root, *path):
    """
    Return the file's absolute path inside the data directory.
    """
    return os.path.join(root_dir(root), "data", *path)


def pathjoin(root, *path):
    """
    Return the file's absolute path inside the environment.
    """
    return os.path.join(base_dir(root), *path)


def base_dir(root):
    """
    Return the environment base directory.
    """
    return os.path.join(root_dir(root), "env")


def root_dir(root):
    """
    Return the project root directory.
    """
    return os.path.abspath(root)
