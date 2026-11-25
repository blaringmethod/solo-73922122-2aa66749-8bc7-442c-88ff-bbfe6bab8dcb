"""Flask 入口：网页路由（表单）+ JSON API（/api/...）。"""
import functools
import os

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import services
from .db import close_db, get_db, init_db
from .services import ServiceError

MAX_CONTENT_LENGTH = 64 * 1024 * 1024  # 两份 2 万行 CSV 绰绰有余


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        DATABASE=os.environ.get("DATABASE", os.path.join("data", "app.db")),
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret"),
        MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,
    )
    if config:
        app.config.update(config)

    init_db(app.config["DATABASE"])
    app.teardown_appcontext(close_db)

    # ----------------------------------------------------------- 网页

    def html_view(fn):
        """网页表单统一异常处理：失败回显 flash，事务已回滚、状态保持不变。"""

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                fn(*args, **kwargs)
            except ServiceError as e:
                flash(e.message, "error")
            return redirect(url_for("index"))

        return wrapper

    @app.get("/")
    def index():
        conn = get_db()
        state = services.get_state(conn)
        furnaces = []
        if state["furnace_col"]:
            try:
                furnaces = services.list_furnaces(conn)
            except ServiceError:
                furnaces = []
        return render_template("index.html", state=state, furnaces=furnaces)

    @app.get("/orders/<int:order_id>")
    def order_page(order_id):
        try:
            detail = services.get_order_detail(get_db(), order_id)
        except ServiceError as e:
            if e.status == 404:
                abort(404)
            raise
        return render_template("order.html", d=detail)

    @app.post("/upload")
    @html_view
    def upload():
        fa = request.files.get("file_a")
        fb = request.files.get("file_b")
        if not fa or not fb or not fa.filename or not fb.filename:
            raise ServiceError("请同时选择旧表、新表两个 CSV 文件")
        header, n_a, n_b = services.load_session(get_db(), fa, fb)
        flash(
            f"已加载：旧表 {n_a} 行、新表 {n_b} 行，共 {len(header)} 列。"
            "此前会话（单据、纸档、结论）已重置。",
            "ok",
        )

    @app.post("/columns")
    @html_view
    def columns():
        services.set_columns(
            get_db(),
            request.form.get("furnace_col", ""),
            request.form.get("size_col", ""),
            request.form.get("pair_col", ""),
        )
        flash("已保存炉次列、尺寸列与配对列。", "ok")

    @app.post("/orders")
    @html_view
    def orders_open():
        order_id, res = services.open_order(
            get_db(),
            request.form.get("furnace", ""),
            request.form.get("tol_lo", ""),
            request.form.get("tol_hi", ""),
            request.form.get("resolution", ""),
            request.form.get("zero_lo", ""),
            request.form.get("zero_hi", ""),
        )
        if res.feasible:
            flash(
                f"炉次 {request.form.get('furnace')} 已开单（#{order_id}）："
                f"超差件数 {res.min_bad}~{res.max_bad} 件。",
                "ok",
            )
        else:
            flash(
                f"炉次 {request.form.get('furnace')} 已开单（#{order_id}）但证据冲突，"
                "无共同可行偏差，已撤下件数区间。",
                "warn",
            )

    @app.post("/orders/<int:order_id>/paper")
    def paper_add(order_id):
        try:
            touched, res = services.upsert_paper(
                get_db(),
                int(request.form["new_row_no"]),
                request.form.get("paper_value", ""),
                order_id,
            )
        except ServiceError as e:
            flash(e.message, "error")
        else:
            note = "已并入本炉" if touched else "已收录（该行不属于本炉，不影响本单结论）"
            if res.feasible:
                flash(
                    f"纸档已保存并同事务重算：{note}；超差件数 {res.min_bad}~{res.max_bad} 件。",
                    "ok",
                )
            else:
                flash("纸档已保存，但重算后证据冲突，已撤下件数区间。", "warn")
        return redirect(url_for("order_page", order_id=order_id))

    # ----------------------------------------------------------- JSON API

    @app.errorhandler(ServiceError)
    def _service_error(e):
        return jsonify(error=e.message), e.status

    @app.post("/api/upload")
    def api_upload():
        fa = request.files.get("file_a")
        fb = request.files.get("file_b")
        if not fa or not fb:
            raise ServiceError("请用 multipart 同时上传 file_a 与 file_b")
        header, n_a, n_b = services.load_session(get_db(), fa, fb)
        return jsonify(ok=True, columns=header, rows_a=n_a, rows_b=n_b)

    @app.post("/api/columns")
    def api_columns():
        data = request.get_json(force=True)
        services.set_columns(
            get_db(),
            str(data.get("furnace_col", "")),
            str(data.get("size_col", "")),
            str(data.get("pair_col", "")),
        )
        return jsonify(ok=True)

    @app.get("/api/furnaces")
    def api_furnaces():
        return jsonify(furnaces=services.list_furnaces(get_db()))

    @app.post("/api/orders")
    def api_orders_open():
        data = request.get_json(force=True)
        order_id, res = services.open_order(
            get_db(),
            str(data.get("furnace", "")),
            str(data.get("tol_lo", "")),
            str(data.get("tol_hi", "")),
            str(data.get("resolution", "")),
            str(data.get("zero_lo", "")),
            str(data.get("zero_hi", "")),
        )
        return jsonify(ok=True, order_id=order_id, conclusion=_result_payload(res))

    @app.get("/api/orders")
    def api_orders():
        return jsonify(orders=services.list_orders(get_db()))

    @app.get("/api/orders/<int:order_id>")
    def api_order_detail(order_id):
        return jsonify(services.get_order_detail(get_db(), order_id))

    @app.post("/api/orders/<int:order_id>/paper")
    def api_paper(order_id):
        data = request.get_json(force=True)
        touched, res = services.upsert_paper(
            get_db(),
            int(data["new_row_no"]),
            str(data.get("paper_value", "")),
            order_id,
        )
        return jsonify(
            ok=True, touched_order=touched, conclusion=_result_payload(res)
        )

    @app.get("/api/paper")
    def api_paper_list():
        return jsonify(paper_records=services.list_paper(get_db()))

    @app.get("/api/state")
    def api_state():
        return jsonify(services.get_state(get_db()))

    return app


def _result_payload(res):
    """audit.AuditResult → 可 JSON 序列化字典（Decimal 转字符串保精度）。"""
    def s(x):
        return None if x is None else str(x)

    return {
        "feasible": res.feasible,
        "feasible_lo": s(res.feasible_lo),
        "feasible_hi": s(res.feasible_hi),
        "total": res.total,
        "fixed_rows": res.fixed_rows,
        "free_rows": res.free_rows,
        "fixed_bad": res.fixed_bad,
        "always_bad_free": res.always_bad_free,
        "free_can_pass": res.free_can_pass,
        "min_bad": res.min_bad,
        "max_bad": res.max_bad,
        "min_bad_deviation": s(res.min_bad_deviation),
        "max_bad_deviation": s(res.max_bad_deviation),
        "conflicts": res.conflicts,
    }


if __name__ == "__main__":
    create_app().run(
        host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), threaded=True
    )
