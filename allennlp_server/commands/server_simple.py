"""
A `Flask <https://palletsprojects.com/p/flask/>`_ server for serving predictions
from a single AllenNLP model. It also includes a very, very bare-bones
web front-end for exploring predictions (or you can provide your own).

    $ allennlp serve --help
    usage: allennlp serve [--help] --archive-path ARCHIVE_PATH --predictor PREDICTOR
                          [--weights-file WEIGHTS_FILE] [--cuda-device CUDA_DEVICE]
                          [-o OVERRIDES] [--static-dir STATIC_DIR] [--title TITLE]
                          [--field-name FIELD_NAME] [-h HOST] [-p PORT]
                          [--include-package INCLUDE_PACKAGE]

    Run a config explorer for allennlp.

    optional arguments:
      --help                show this help message and exit
      --archive-path ARCHIVE_PATH
                            path to trained archive file
      --predictor PREDICTOR
                            name of predictor
      --weights-file WEIGHTS_FILE
                            a path that overrides which weights file to use
      --cuda-device CUDA_DEVICE
                            id of GPU to use (if any)
      -o OVERRIDES, --overrides OVERRIDES
                            a JSON structure used to override the experiment configuration
      --static-dir STATIC_DIR
                            serve index.html from this directory
      --title TITLE
                            change the default page title (default = AllenNLP Demo)
      --field-name FIELD_NAME
                            field names to include in the demo
      -h HOST, --host HOST  interface to serve the wizard on (default = 127.0.0.1)
      -p PORT, --port PORT  port to serve the wizard on (default = 8000)
      --include-package INCLUDE_PACKAGE
                            additional packages to include
"""
import argparse
import json
import logging
import os
import sys
from string import Template
from typing import List, Callable

from allennlp.commands import Subcommand
from allennlp.common import JsonDict
from allennlp.common.checks import check_for_gpu
from allennlp.models.archival import load_archive
from allennlp.predictors import Predictor
from flask import Flask, request, Response, jsonify, send_file, send_from_directory
from flask_cors import CORS
from gevent.pywsgi import WSGIServer

from allennlp_server.config_explorer.config_explorer import ServerError

logger = logging.getLogger(__name__)


def make_app(
    predictor: Predictor,
    field_names: List[str] = None,
    static_dir: str = None,
    sanitizer: Callable[[JsonDict], JsonDict] = None,
    title: str = "AllenNLP Demo",
) -> Flask:
    """
    Creates a Flask app that serves up the provided ``Predictor``
    along with a front-end for interacting with it.

    If you want to use the built-in bare-bones HTML, you must provide the
    field names for the inputs (which will be used both as labels
    and as the keys in the JSON that gets sent to the predictor).

    If you would rather create your own HTML, call it index.html
    and provide its directory as ``static_dir``. In that case you
    don't need to supply the field names -- that information should
    be implicit in your demo site. (Probably the easiest thing to do
    is just start with the bare-bones HTML and modify it.)

    In addition, if you want somehow transform the JSON prediction
    (e.g. by removing probabilities or logits)
    you can do that by passing in a ``sanitizer`` function.
    """
    if static_dir is not None:
        static_dir = os.path.abspath(static_dir)
        if not os.path.exists(static_dir):
            logger.error("app directory %s does not exist, aborting", static_dir)
            sys.exit(-1)
    elif static_dir is None and field_names is None:
        print(
            "Neither build_dir nor field_names passed. Demo won't render on this port.\n"
            "You must use nodejs + react app to interact with the server."
        )

    app = Flask(__name__)

    @app.errorhandler(ServerError)
    def handle_invalid_usage(error: ServerError) -> Response:
        response = jsonify(error.to_dict())
        response.status_code = error.status_code
        return response

    @app.route("/")
    def index() -> Response:
        if static_dir is not None:
            return send_file(os.path.join(static_dir, "index.html"))
        else:
            html = _html(title, field_names)
            return Response(response=html, status=200)

    @app.route("/predict", methods=["POST", "OPTIONS"])
    def predict() -> Response:
        """make a prediction using the specified model and return the results"""
        if request.method == "OPTIONS":
            return Response(response="", status=200)

        data = request.get_json()

        prediction = predictor.predict_json(data)
        if sanitizer is not None:
            prediction = sanitizer(prediction)

        log_blob = {"inputs": data, "outputs": prediction}
        logger.info("prediction: %s", json.dumps(log_blob))

        return jsonify(prediction)

    @app.route("/predict_batch", methods=["POST", "OPTIONS"])
    def predict_batch() -> Response:
        """make a prediction using the specified model and return the results"""
        if request.method == "OPTIONS":
            return Response(response="", status=200)

        data = request.get_json()

        prediction = predictor.predict_batch_json(data)
        if sanitizer is not None:
            prediction = [sanitizer(p) for p in prediction]

        return jsonify(prediction)

    @app.route("/<path:path>")
    def static_proxy(path: str) -> Response:
        if static_dir is not None:
            return send_from_directory(static_dir, path)
        else:
            raise ServerError("static_dir not specified", 404)

    return app


def _get_predictor(args: argparse.Namespace) -> Predictor:
    check_for_gpu(args.cuda_device)
    archive = load_archive(
        args.archive_path,
        weights_file=args.weights_file,
        cuda_device=args.cuda_device,
        overrides=args.overrides,
    )

    return Predictor.from_archive(archive, args.predictor)


class SimpleServer(Subcommand):
    def add_subparser(
        self, name: str, parser: argparse._SubParsersAction
    ) -> argparse.ArgumentParser:
        description = """Serve up a simple model."""
        subparser = parser.add_parser(
            name, description=description, help="Serve up a simple model.",
        )

        subparser.add_argument(
            "--archive-path", type=str, required=True, help="path to trained archive file",
        )

        subparser.add_argument("--predictor", type=str, required=True, help="name of predictor")

        subparser.add_argument(
            "--weights-file", type=str, help="a path that overrides which weights file to use",
        )

        subparser.add_argument(
            "--cuda-device", type=int, default=-1, help="id of GPU to use (if any)"
        )

        subparser.add_argument(
            "-o",
            "--overrides",
            type=str,
            default="",
            help="a JSON structure used to override the experiment configuration",
        )

        subparser.add_argument(
            "--static-dir", type=str, help="serve index.html from this directory"
        )

        subparser.add_argument(
            "--title", type=str, help="change the default page title", default="AllenNLP Demo",
        )

        subparser.add_argument(
            "--field-name",
            type=str,
            action="append",
            dest="field_names",
            help="field names to include in the demo",
        )

        subparser.add_argument(
            "-h", "--host", type=str, default="127.0.0.1", help="interface to serve the wizard on",
        )

        subparser.add_argument(
            "-p", "--port", type=int, default=8000, help="port to serve the demo on"
        )

        subparser.set_defaults(func=serve)

        return subparser


def serve(args: argparse.Namespace) -> None:
    predictor = _get_predictor(args)

    app = make_app(
        predictor=predictor,
        field_names=args.field_names,
        static_dir=args.static_dir,
        title=args.title,
    )
    CORS(app)

    http_server = WSGIServer((args.host, args.port), app)
    print(f"Model loaded, serving demo on http://{args.host}:{args.port}")
    http_server.serve_forever()


#
# HTML and Templates for the default bare-bones app are below
#


_PAGE_TEMPLATE = Template(
    """
<html>
    <head>
        <title>
            $title
        </title>
        <style>
            $css
        </style>
    </head>
    <body>
        <div class="pane-container">
            <div class="pane model">
                <div class="pane__left model__input">
                    <div class="model__content">
                        <h2><span>$title</span></h2>
                        <div class="model__content">
                            $inputs
                            <div class="form__field form__field--btn">
                                <button type="button" class="btn btn--icon-disclosure" onclick="predict()">
                                    Predict
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="pane__right model__output model__output--empty">
                    <div class="pane__thumb"></div>
                    <div class="model__content">
                        <div id="output" class="output">
                            <div class="placeholder">
                                <div class="placeholder__content">
                                    <p>Run model to view results</p>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </body>
    <script>
    function predict() {
        var quotedFieldList = $qfl;
        var data = {};
        quotedFieldList.forEach(function(fieldName) {
            data[fieldName] = document.getElementById("input-" + fieldName).value;
        })

        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/predict');
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.onload = function() {
            if (xhr.status == 200) {
                // If you want a more impressive visualization than just
                // outputting the raw JSON, change this part of the code.
                var htmlResults = "<pre>" + JSON.stringify(JSON.parse(xhr.responseText), null, 2) + "</pre>";

                document.getElementById("output").innerHTML = htmlResults;
            }
        };
        xhr.send(JSON.stringify(data));
    }
    </script>
</html>
"""
)

_SINGLE_INPUT_TEMPLATE = Template(
    """
        <div class="form__field">
            <label for="input-$field_name">$field_name</label>
            <input type="text" id="input-$field_name" type="text" required value placeholder="input goes here">
        </div>
"""
)

_CSS = """
body,
html {
  min-width: 48em;
  background: #f9fafc;
  font-size: 16px
}

* {
  font-family: sans-serif;
  color: #232323
}

section {
  background: #fff
}

code,
code span,
pre,
.output {
  font-family: 'Roboto Mono', monospace!important
}

code {
  background: #f6f8fa
}

li,
p,
td,
th {
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  font-size: 1.125em;
  line-height: 1.5em;
  margin: 1.2em 0
}

pre {
  margin: 2em 0
}

h1,
h2 {
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  font-weight: 300
}

h2 {
  font-size: 2em;
  color: rgba(35, 35, 35, .75)
}

img {
  max-width: 100%
}

hr {
  display: block;
  border: none;
  height: .375em;
  background: #f6f8fa
}

blockquote,
hr {
  margin: 2.4em 0
}

.btn {
  text-decoration: none;
  cursor: pointer;
  text-transform: uppercase;
  font-size: 1em;
  margin: 0;
  -moz-appearance: none;
  -webkit-appearance: none;
  border: none;
  color: #fff!important;
  display: block;
  background: #2085bc;
  padding: .9375em 3.625em;
  -webkit-transition: background-color .2s ease, opacity .2s ease;
  transition: background-color .2s ease, opacity .2s ease
}

.btn.btn--blue {
  background: #2085bc
}

.btn:focus,
.btn:hover {
  background: #40affd;
  outline: 0
}

.btn:focus {
  box-shadow: 0 0 1.25em rgba(50, 50, 150, .05)
}

.btn:active {
  opacity: .66;
  background: #2085bc;
  -webkit-transition-duration: 0s;
  transition-duration: 0s
}

.btn:disabled,
.btn:disabled:active,
.btn:disabled:hover {
  cursor: default;
  background: #d0dae3
}

form {
  display: block
}

.form__field {
  -webkit-transition: margin .2s ease;
  transition: margin .2s ease
}

.form__field+.form__field {
  margin-top: 2.5em
}

.form__field label {
  display: block;
  font-weight: 600;
  font-size: 1.125em
}

.form__field label+* {
  margin-top: 1.25em
}

.form__field input[type=text],
.form__field textarea {
  -moz-appearance: none;
  -webkit-appearance: none;
  width: 100%;
  font-size: 1em;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  padding: .8125em 1.125em;
  color: #232323;
  border: .125em solid #d4dce2;
  display: block;
  box-sizing: border-box;
  -webkit-transition: background-color .2s ease, color .2s ease, border-color .2s ease, opacity .2s ease;
  transition: background-color .2s ease, color .2s ease, border-color .2s ease, opacity .2s ease
}

.form__field input[type=text]::-webkit-input-placeholder,
.form__field textarea::-webkit-input-placeholder {
  color: #b4b4b4
}

.form__field input[type=text]:-moz-placeholder,
.form__field textarea:-moz-placeholder {
  color: #b4b4b4
}

.form__field input[type=text]::-moz-placeholder,
.form__field textarea::-moz-placeholder {
  color: #b4b4b4
}

.form__field input[type=text]:-ms-input-placeholder,
.form__field textarea:-ms-input-placeholder {
  color: #b4b4b4
}

.form__field input[type=text]:focus,
.form__field textarea:focus {
  outline: 0;
  border-color: #63a7d4;
  box-shadow: 0 0 1.25em rgba(50, 50, 150, .05)
}

.form__field textarea {
  resize: vertical;
  min-height: 8.25em
}

.form__field .btn {
  -webkit-user-select: none;
  -moz-user-select: none;
  -ms-user-select: none;
  user-select: none;
  -webkit-touch-callout: none
}

.form__field--btn {
  display: -webkit-box;
  display: -ms-flexbox;
  display: -webkit-flex;
  display: flex;
  -webkit-flex-direction: row;
  -ms-flex-direction: row;
  -webkit-box-orient: horizontal;
  -webkit-box-direction: normal;
  flex-direction: row;
  -webkit-justify-content: flex-end;
  -ms-justify-content: flex-end;
  -webkit-box-pack: end;
  -ms-flex-pack: end;
  justify-content: flex-end
}

@media screen and (max-height:760px) {
  .form__instructions {
    margin: 1.875em 0 1.125em
  }
  .form__field:not(.form__field--btn)+.form__field:not(.form__field--btn) {
    margin-top: 1.25em
  }
}

body,
html {
  width: 100%;
  height: 100%;
  margin: 0;
  padding: 0;
  font-family: 'Source Sans Pro', sans-serif
}

h1 {
  font-weight: 300
}

.model__output {
  background: #fff
}

.model__output.model__output--empty {
  background: 0 0
}

.placeholder {
  width: 100%;
  height: 100%;
  display: -webkit-box;
  display: -ms-flexbox;
  display: -webkit-flex;
  display: flex;
  -webkit-align-items: center;
  -ms-flex-align: center;
  -webkit-box-align: center;
  align-items: center;
  -webkit-justify-content: center;
  -ms-justify-content: center;
  -webkit-box-pack: center;
  -ms-flex-pack: center;
  justify-content: center;
  -webkit-user-select: none;
  -moz-user-select: none;
  -ms-user-select: none;
  user-select: none;
  -webkit-touch-callout: none;
  cursor: default
}

.placeholder .placeholder__content {
  display: -webkit-box;
  display: -ms-flexbox;
  display: -webkit-flex;
  display: flex;
  -webkit-flex-direction: column;
  -ms-flex-direction: column;
  -webkit-box-orient: vertical;
  -webkit-box-direction: normal;
  flex-direction: column;
  -webkit-align-items: center;
  -ms-flex-align: center;
  -webkit-box-align: center;
  align-items: center;
  text-align: center
}

.placeholder svg {
  display: block
}

.placeholder svg.placeholder__empty,
.placeholder svg.placeholder__error {
  width: 6em;
  height: 3.625em;
  fill: #e1e5ea;
  margin-bottom: 2em
}

.placeholder svg.placeholder__error {
  width: 4.4375em;
  height: 4em
}

.placeholder p {
  font-size: 1em;
  margin: 0;
  padding: 0;
  color: #9aa8b2
}

.placeholder svg.placeholder__working {
  width: 3.4375em;
  height: 3.4375em;
  -webkit-animation: working 1s infinite linear;
  animation: working 1s infinite linear
}

@-webkit-keyframes working {
  0% {
    -webkit-transform: rotate(0deg)
  }
  100% {
    -webkit-transform: rotate(360deg)
  }
}

@keyframes working {
  0% {
    -webkit-transform: rotate(0deg);
    -ms-transform: rotate(0deg);
    transform: rotate(0deg)
  }
  100% {
    -webkit-transform: rotate(360deg);
    -ms-transform: rotate(360deg);
    transform: rotate(360deg)
  }
}

.model__content {
  padding: 1.875em 2.5em;
  margin: auto;
  -webkit-transition: padding .2s ease;
  transition: padding .2s ease
}

.model__content:not(.model__content--srl-output) {
  max-width: 61.25em
}

.model__content h2 {
  margin: 0;
  padding: 0;
  font-size: 1em
}

.model__content h2 span {
  font-size: 2em;
  color: rgba(35, 35, 35, .75)
}

.model__content h2 .tooltip,
.model__content h2 span {
  vertical-align: top
}

.model__content h2 span+.tooltip {
  margin-left: .4375em
}

.model__content>h2:first-child {
  margin: -.25em 0 0 -.03125em
}

.model__content__summary {
  font-size: 1em;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  padding: 1.25em;
  background: #f6f8fa
}

@media screen and (min-height:800px) {
  .model__content {
    padding-top: 4.6vh;
    padding-bottom: 4.6vh
  }
}

.pane-container {
  display: -webkit-box;
  display: -ms-flexbox;
  display: -webkit-flex;
  display: flex;
  -webkit-flex-direction: column;
  -ms-flex-direction: column;
  -webkit-box-orient: vertical;
  -webkit-box-direction: normal;
  flex-direction: column;
  height: 100%
}

.pane {
  display: -webkit-box;
  display: -ms-flexbox;
  display: -webkit-flex;
  display: flex;
  -webkit-flex-direction: row;
  -ms-flex-direction: row;
  -webkit-box-orient: horizontal;
  -webkit-box-direction: normal;
  flex-direction: row;
  position: relative;
  -webkit-box-flex: 2;
  -webkit-flex: 2;
  -ms-flex: 2;
  flex: 2;
  height: auto;
  min-height: 100%;
  min-height: 34.375em
}

.pane__left,
.pane__right {
  width: 100%;
  height: 100%;
  -webkit-align-self: stretch;
  -ms-flex-item-align: stretch;
  align-self: stretch;
  min-width: 24em;
  min-height: 34.375em
}

.pane__left {
  height: auto;
  min-height: 100%
}

.pane__right {
  width: 100%;
  overflow: auto;
  height: auto;
  min-height: 100%
}

.pane__right .model__content.model__content--srl-output {
  display: inline-block;
  margin: auto
}

.pane__thumb {
  height: auto;
  min-height: 100%;
  margin-left: -.625em;
  position: absolute;
  width: 1.25em
}

.pane__thumb:after {
  display: block;
  position: absolute;
  height: 100%;
  top: 0;
  content: "";
  width: .25em;
  background: #e1e5ea;
  left: .5em
}
"""


def _html(title: str, field_names: List[str]) -> str:
    """
    Returns bare bones HTML for serving up an input form with the
    specified fields that can render predictions from the configured model.
    """
    inputs = "".join(
        _SINGLE_INPUT_TEMPLATE.substitute(field_name=field_name) for field_name in field_names
    )

    quoted_field_names = (f"'{field_name}'" for field_name in field_names)
    quoted_field_list = f"[{','.join(quoted_field_names)}]"

    return _PAGE_TEMPLATE.substitute(title=title, css=_CSS, inputs=inputs, qfl=quoted_field_list)
