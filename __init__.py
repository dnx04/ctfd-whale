import fcntl
import os
import traceback
import warnings

from flask import Blueprint, render_template, request, session
from flask_apscheduler import APScheduler

from CTFd.api import CTFd_API_v1
from CTFd.plugins import (
    register_admin_plugin_menu_bar,
    register_plugin_assets_directory,
)
from CTFd.plugins.challenges import CHALLENGE_CLASSES
from CTFd.utils import get_config, set_config
from CTFd.utils.decorators import admins_only
from CTFd.utils.logging import log

from .api import AdminContainers, admin_namespace, user_namespace
from .challenge_type import DynamicValueDockerChallenge
from .utils.checks import WhaleChecks
from .utils.control import ControlUtil
from .utils.db import DBContainer
from .utils.docker import DockerUtils
from .utils.exceptions import WhaleWarning
from .utils.routers import Router
from .utils.setup import setup_default_configs


def load(app):
    app.config["RESTX_ERROR_404_HELP"] = False
    # upgrade()
    plugin_name = __name__.split(".")[-1]
    set_config("whale:plugin_name", plugin_name)
    app.db.create_all()
    if not get_config("whale:setup"):
        setup_default_configs()

    register_plugin_assets_directory(
        app,
        base_path=f"/plugins/{plugin_name}/assets",
        endpoint="plugins.ctfd-whale.assets",
    )
    register_admin_plugin_menu_bar(
        title="Whale", route="/plugins/ctfd-whale/admin/settings"
    )

    DynamicValueDockerChallenge.templates = {
        "create": f"/plugins/{plugin_name}/assets/create.html",
        "update": f"/plugins/{plugin_name}/assets/update.html",
        "view": f"/plugins/{plugin_name}/assets/view.html",
    }
    DynamicValueDockerChallenge.scripts = {
        "create": "/plugins/ctfd-whale/assets/create.js",
        "update": "/plugins/ctfd-whale/assets/update.js",
        "view": "/plugins/ctfd-whale/assets/view.js",
    }
    CHALLENGE_CLASSES["dynamic_docker"] = DynamicValueDockerChallenge

    page_blueprint = Blueprint(
        "ctfd-whale",
        __name__,
        template_folder="templates",
        static_folder="assets",
        url_prefix="/plugins/ctfd-whale",
    )
    CTFd_API_v1.add_namespace(admin_namespace, path="/plugins/ctfd-whale/admin")
    CTFd_API_v1.add_namespace(user_namespace, path="/plugins/ctfd-whale")

    worker_config_commit = None

    @page_blueprint.route("/admin/settings")
    @admins_only
    def admin_list_configs():
        nonlocal worker_config_commit
        errors = WhaleChecks.perform()
        if not errors and get_config("whale:refresh") != worker_config_commit:
            worker_config_commit = get_config("whale:refresh")
            DockerUtils.init()
            Router.reset()
            set_config("whale:refresh", "false")
        return render_template("whale_config.html", errors=errors)

    @page_blueprint.route("/admin/containers")
    @admins_only
    def admin_list_containers():
        result = AdminContainers.get()
        view_mode = request.args.get("mode", session.get("view_mode", "list"))
        session["view_mode"] = view_mode
        return render_template(
            "whale_containers.html",
            plugin_name=plugin_name,
            containers=result["data"]["containers"],
            pages=result["data"]["pages"],
            curr_page=abs(request.args.get("page", 1, type=int)),
            curr_page_start=result["data"]["page_start"],
        )

    @page_blueprint.route("/admin/upload", methods=["GET", "POST"])
    @admins_only
    def admin_upload_image():
        global filepath
        if request.method == "POST":
            name = request.args.get("name")
            if not name:
                return {"success": False, "message": "Missing parameter: name"}, 400
            tag = request.args.get("tag")
            if not tag:
                return {"success": False, "message": "Missing parameter: tag"}, 400
            # 检查文件是否存在于请求中
            if "image" not in request.files:
                return {"success": False, "message": "Image file not exists"}, 500
            file = request.files["image"]
            if file.filename == "":
                return {"success": False, "message": "The mirror file is empty"}, 500
            if file:
                try:
                    filepath = os.path.join(app.config["UPLOAD_FOLDER"], file.filename)
                    file.save(filepath)
                    log(
                        "whale",
                        "[{date}] [CTFd Whale] {ip} The uploaded mirror file ({name}:{tag}) is saved to:{filepath}",
                        name=name,
                        tag=tag,
                        filepath=filepath,
                    )
                    try:
                        DockerUtils.client.images.get(name + ":" + tag)
                        DockerUtils.client.api.remove_image(name + ":" + tag)
                    except Exception:
                        pass
                    DockerUtils.client.api.import_image_from_file(
                        filepath, repository=name, tag=tag
                    )
                    log(
                        "whale",
                        "[{date}] [CTFd Whale] {name}:{tag}Import completed.",
                        name=name,
                        tag=tag,
                    )
                    # 删除上传的文件
                    os.remove(filepath)
                    return {"success": True, "message": "Image upload completed"}, 200
                except Exception as e:
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
                    traceback_str = "".join(traceback.format_tb(e.__traceback__))
                    log(
                        "whale",
                        "[{date}] [CTFd Whale] {name} Image upload failed. {error}\n{tb}",
                        name=(name + ":" + tag),
                        error=e,
                        tb=traceback_str,
                    )
                    return {
                        "success": False,
                        "message": "Image upload failed. <br>" + str(e),
                    }, 500

        return render_template("whale_upload.html")

    @page_blueprint.route("/admin/image-update")
    @admins_only
    def admin_image_update():
        try:
            # 获取GET请求中的name参数
            name = request.args.get("name")
            log(
                "whale",
                "[{date}] [CTFd Whale] Trying to update the image {name}.",
                name=name,
            )
            DockerUtils.client.api.pull(name)
            # 返回HTTP状态码200
            log(
                "whale",
                "[{date}] [CTFd Whale] {name} image update successful.",
                name=name,
            )
            return {"success": True, "message": "Image update successful"}, 200
        except Exception as e:
            name = request.args.get("name")
            traceback_str = "".join(traceback.format_tb(e.__traceback__))
            log(
                "whale",
                "[{date}] [CTFd Whale] {name} Image update failed. {error}\n{tb}",
                name=name,
                error=e,
                tb=traceback_str,
            )
            return {
                "success": False,
                "message": "Image update failed. <br>" + str(e.__cause__),
            }, 200

    @page_blueprint.route("/admin/getLog")
    @admins_only
    def admin_get_log():
        id = request.args.get("id")
        tail = request.args.get("tail", 1000)
        docker_client = DockerUtils.client
        if id:
            try:
                container = docker_client.containers.get(id)
                logs_text = container.logs(
                    stdout=True, stderr=True, stream=False, tail=tail
                )
                return {"success": True, "message": logs_text.decode("utf-8")}, 200
            except Exception as e:
                return {
                    "success": False,
                    "message": "Log acquisition failed: <br>" + str(e.__cause__),
                }, 200
        return {"success": False, "message": "Log acquisition failed."}, 200

    @page_blueprint.route("/admin/docker")
    @admins_only
    def admin_list_docker():
        containers = DockerUtils.client.containers.list(all=True)
        return render_template(
            "whale_docker.html", plugin_name=plugin_name, containers=containers
        )

    @page_blueprint.route("/admin/viewLog")
    @admins_only
    def admin_view_log():
        return render_template("whale_log.html", plugin_name=plugin_name)

    def auto_clean_container():
        with app.app_context():
            results = DBContainer.get_all_expired_container()
            for r in results:
                ControlUtil.try_remove_container(r.user_id)

    app.register_blueprint(page_blueprint)

    try:
        Router.check_availability()
        DockerUtils.init()
    except Exception:
        warnings.warn(
            "Initialization Failed. Please check your configs.",
            WhaleWarning,
            stacklevel=2,
        )

    try:
        lock_file = open("/tmp/ctfd_whale.lock", "w")
        lock_fd = lock_file.fileno()
        fcntl.lockf(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        scheduler = APScheduler()
        scheduler.init_app(app)
        scheduler.start()
        scheduler.add_job(
            id="whale-auto-clean",
            func=auto_clean_container,
            trigger="interval",
            seconds=10,
        )

        print("[CTFd Whale] Started successfully")
    except IOError:
        pass
