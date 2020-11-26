import svgwrite
import logging
import math
import tempfile
import cairosvg
import base64
import html

from .job_info import get_human_time

logger = logging.getLogger(__name__)

STYLES = """
      rect              { stroke-width: 1; stroke-opacity: 0; }
      rect.background   { fill: rgb(255,255,255); }
      rect.box          { fill: rgb(240,240,240); stroke: rgb(192,192,192); }
      line       { stroke: rgb(64,64,64); stroke-width: 1; }
      line.min5  { stroke: rgb(150,150,150); stroke-width: 1; }
      line.min60 { stroke: rgb(150,150,150); stroke-width: 2; }
      line.min01 { stroke: rgb(224,224,224); stroke-width: 1; }

      rect.queue        { fill: rgb(148,111,97); fill-opacity: 0.3; }

      rect.aborted       { fill: rgb(190,200,183); fill-opacity: 0.7; }
      rect.success       { fill: rgb(170,255,170); fill-opacity: 0.7; }
      rect.infra_failure { fill: rgb(249,170,255); fill-opacity: 0.7; }
      rect.failure       { fill: rgb(255,170,170); fill-opacity: 0.7; }
      rect.unstable      { fill: rgb(255,204,170); fill-opacity: 0.7; }
      rect.other         { fill: rgb(204,204,204); fill-opacity: 0.7; }
      rect.in_progress   { fill: rgb(135,205,222); fill-opacity: 0.7; }

      rect.pipe_aborted      { stroke: rgb(190,200,183); stroke-width: 5; stroke-opacity: 0.7; fill: rgb(190,200,183); fill-opacity: 0.3; }
      rect.pipe_success      { stroke: rgb(170,255,170); stroke-width: 5; stroke-opacity: 0.7; fill: rgb(170,255,170); fill-opacity: 0.3; }
      rect.pipe_failure      { stroke: rgb(255,170,170); stroke-width: 5; stroke-opacity: 0.7; fill: rgb(255,170,170); fill-opacity: 0.3; }
      rect.pipe_unstable     { stroke: rgb(255,204,170); stroke-width: 5; stroke-opacity: 0.7; fill: rgb(255,204,170); fill-opacity: 0.3; }
      rect.pipe_other        { stroke: rgb(204,204,204); stroke-width: 5; stroke-opacity: 0.7; fill: rgb(204,204,204); fill-opacity: 0.3; }
      rect.pipe_in_progress  { stroke: rgb(135,205,222); stroke-width: 5; stroke-opacity: 0.7; fill: rgb(135,205,222); fill-opacity: 0.3; }

      rect.type         { fill: rgb(50,50,50); fill-opacity: 0.7; }
      rect.type_scm     { fill: rgb(255,208,147); fill-opacity: 0.7; }
      rect.type_docker  { fill: rgb(147,214,255); fill-opacity: 0.7; }
      rect.type_build   { fill: rgb(255,147,180); fill-opacity: 0.7; }
      rect.type_test    { fill: rgb(167,147,255); fill-opacity: 0.7; }
      rect.type_archive { fill: rgb(147,255,221); fill-opacity: 0.7; }
      rect.type_sca     { fill: rgb(147,201,181); fill-opacity: 0.7; }
      rect.type_init    { fill: #97F2F3; fill-opacity: 0.7; }

      text       { font-family: Verdana, Helvetica; font-size: 14px; }
      text.left  { font-family: Verdana, Helvetica; font-size: 14px; text-anchor: start; }
      text.right { font-family: Verdana, Helvetica; font-size: 14px; text-anchor: end; }
      text.min   { font-size: 10px; }
      text.time  { font-size: 5px; }
"""

HTML_TMPL = """<!DOCTYPE html>
<html>
<head>
    <title>%s</title>
    <meta charset="UTF-8">
    <style type="text/css">
        .tooltip {
            display: none;
            position: absolute;
            border: 1px solid rgba(50, 50, 50, .8);
            border-radius: 3px;
            background: rgba(50, 50, 50, .7);
            color: white;
            font-family: "Arial";
            font-size: small;
            padding: 5px;
        }
        .description {
            padding: 4px;
            padding-left: 6px;
            background: rgba(50, 50, 50, .2);
            border-radius: 3px;
            margin: 2px;
        }
        .description a {
            color: white;
        }
        .failure-cause {
            padding: 4px;
            padding-left: 6px;
            background: rgba(50, 50, 50, .2);
            border-radius: 3px;
            margin: 2px;
            margin-left: 6px;
            border-left: 2px solid #eee;
            font-family: monospace;
        }
        .sections {
            padding: 4px;
            padding-left: 6px;
            background: rgba(50, 50, 50, .2);
            border-radius: 3px;
            margin: 2px;
            margin-left: 6px;
            border-left: 2px solid #eee;
            font-family: monospace;
        }
        .sections .error {
            color: #ffbfbf;
            font-weight: 700;
        }
    </style>
    %s
</head>
<body>
    <div result="%s">
        <img usemap="#map" class="map" src="%s" />
        <map name="map">
            %s
        </map>
        %s
    <div>
</body>
</html>"""

MAPHIGHLIGHT_SCRIPT = """
<script type="text/javascript" src="https://unpkg.com/jquery"></script>
<script type="text/javascript" src="https://cdn.rawgit.com/kemayo/maphilight/master/jquery.maphilight.min.js"></script>
<script type="text/javascript">
$(function() {
    $('.map').maphilight({
        stroke: false,
        fillOpacity: 0.1
    });
    $('area').mousemove(function(e) {
        var tooltip = $(this).data("tooltip");
        var left = e.pageX + 5;
        var top = e.pageY + 5;
        if( (top + $(tooltip).height()) > window.innerHeight)
        {
            top = e.pageY - 15 - $(tooltip).height();
        }
        $(tooltip).css('top', top);
        $(tooltip).css('left', left);
        $(tooltip).fadeIn();
    });
    $('area').mouseout(function(e) {
        var tooltip = $(this).data("tooltip");
        if(tooltip) {
            $(tooltip).fadeOut();
        }
    });
});
</script>"""


class SvgPrinter:
    def __init__(self, job_info):
        self.job_info = job_info

        self.margin = 20
        self.extra_width = 200

        # Options
        self.show_time = True
        self.show_infobox = True

        self.build_padding = 5
        self.build_height = 30
        self.section_height = 2
        self.minute_width = 10
        self.min_width = 5
        self.base_timestamp = None

        self.rect_builds = {}
        self.box_height = None
        self.box_width = None
        self.total_height = None
        self.total_width = None

        self.__dwg = None
        self.current_pos = None

        self.all_builds = self.job_info.all_builds
        self.build_result = self.job_info.result
        self.duration = self.job_info.duration
        self.max_duration = 0

    def determine_max_duration(self):
        self.max_duration = self.duration
        if self.build_result == "IN_PROGRESS":
            for build in self.all_builds:
                self.max_duration = max(
                    self.max_duration,
                    build.start + build.duration - self.base_timestamp,
                )

        self.max_duration = (
            math.ceil(self.max_duration / 1000 / 60 / 5) * 5
        )  # in minutes, rounded up

    def __determine_sizes(self):

        self.base_timestamp = self.job_info.start

        # Height based on number of builds to show

        self.box_height = self.build_height * len(self.all_builds)
        self.total_height = 2 * self.margin + self.box_height

        # Width based on duration
        self.determine_max_duration()

        self.box_width = self.minute_width * self.max_duration
        if self.max_duration == 0:
            self.box_width = self.min_width
        self.total_width = 2 * self.margin + self.box_width + self.extra_width

        logger.debug("Total: %d x %d", self.total_height, self.total_width)

    def __render_grid(self):
        dwg = self.__dwg

        self.current_pos = self.margin

        dwg.add(
            dwg.rect(
                insert=(self.margin, self.margin),
                size=(self.box_width, self.box_height),
                class_="box",
            )
        )

        for x in range(0, self.max_duration):

            class_name = "min01"
            if x % 5 == 0:
                class_name = "min5"
                if x % 60 == 0:
                    class_name = "min60"

                dwg.add(
                    dwg.text(
                        "%dmin" % x,
                        insert=(self.current_pos, self.margin - 5),
                        class_="min",
                    )
                )

            dwg.add(
                dwg.line(
                    start=(self.current_pos, self.margin),
                    end=(self.current_pos, self.total_height - self.margin),
                    class_=class_name,
                )
            )

            self.current_pos += self.minute_width

    def __render_section(self, build, section, build_index):
        dwg = self.__dwg

        offset = (section.start - self.base_timestamp) / 1000 / 60
        if offset < 0:
            offset = 0
        offset_px = offset * self.minute_width

        duration = section.duration / 1000 / 60
        if not section.end:
            if not section.parent:
                if build.end:
                    duration = (build.end - section.start) / 1000 / 60
            else:
                parent_section = section.parent
                while parent_section is not None:
                    if parent_section.end:
                        duration = (parent_section.end - section.start) / 1000 / 60
                        break
                    parent_section = parent_section.parent
        duration_px = duration * self.minute_width

        class_name = "type"
        if section.type:
            class_name = "type_%s" % section.type

        section_index = section.parents_cnt
        if section_index >= 4:
            section_index = 4

        x = self.margin + offset_px
        y = self.margin + build_index * self.build_height

        dwg.add(
            dwg.rect(
                insert=(x, y + self.build_height - self.build_padding),
                size=(duration_px, (1 + section_index) * self.section_height),
                class_=class_name,
            )
        )

    def __render_queue(self, build, build_index):
        dwg = self.__dwg

        if not build.queueing_duration:
            return

        offset = (
            (build.start - build.queueing_duration - self.base_timestamp) / 1000 / 60
        )
        offset_px = offset * self.minute_width

        duration = build.queueing_duration / 1000 / 60
        duration_px = duration * self.minute_width

        class_name = "queue"

        x = self.margin + offset_px
        y = self.margin + build_index * self.build_height

        dwg.add(
            dwg.rect(
                insert=(x, y + self.build_padding),
                size=(duration_px, self.build_height - 2 * self.build_padding),
                class_=class_name,
            )
        )

    def __render_build(self, build, index):
        dwg = self.__dwg

        self.__render_queue(build, index)

        offset = 0
        if build.start:
            offset = (build.start - self.base_timestamp) / 1000 / 60
        offset_px = offset * self.minute_width

        duration = build.duration / 1000 / 60
        if build.result == "IN_PROGRESS":
            duration = self.max_duration - offset
        duration_px = duration * self.minute_width
        if duration_px < self.min_width:
            duration_px = self.min_width

        class_name = "other"
        if build.result == "SUCCESS":
            class_name = "success"
        elif build.result == "ABORTED":
            class_name = "aborted"
        elif build.result == "INFRA_FAILURE":
            class_name = "infra_failure"
        elif build.result == "FAILURE":
            class_name = "failure"
        elif build.result == "UNSTABLE":
            class_name = "unstable"
        elif build.result == "IN_PROGRESS":
            class_name = "in_progress"

        if build.job_type in ["pipeline", "buildFlow", "matrixBuild", "matrixRun"]:
            class_name = "pipe_%s" % class_name

        x = self.margin + offset_px
        y = self.margin + index * self.build_height

        build_id = "%s#%s" % (build.job_name, build.build_number)

        build_r = {
            "build": build,
            "insert": (x, y + self.build_padding),
            "size": (duration_px, self.build_height - 2 * self.build_padding),
        }
        self.rect_builds[build_id] = build_r
        dwg.add(
            dwg.rect(insert=build_r["insert"], size=build_r["size"], class_=class_name)
        )

        if build.sections:
            for section in build.sections:
                self.__render_section(build, section, index)

        build_info = ""
        if build.stage:
            build_info = "[%s] " % build.stage
        build_info += build_id
        dwg.add(
            dwg.text(
                build_info,
                insert=(x + 5, y + self.build_height - self.build_padding - 8),
                class_="min",
            )
        )

        if self.show_time:
            queue_time = get_human_time(build.queueing_duration)
            exec_time = get_human_time(build.duration)
            build_time = "[queue: %s; build: %s]" % (queue_time, exec_time)

            dwg.add(
                dwg.text(
                    build_time,
                    insert=(x + 5, y + self.build_height - self.build_padding - 1),
                    class_="time",
                )
            )

    def __render_builds(self):
        current_idx = 0
        for build in self.all_builds:
            self.__render_build(build, current_idx)
            current_idx += 1

    def print_svg(self, output):
        self.__determine_sizes()

        self.__dwg = svgwrite.Drawing(
            filename=output, size=(self.total_width, self.total_height), debug=True
        )

        dwg = self.__dwg

        # Add styles
        dwg.defs.add(dwg.style(STYLES))

        # Background
        dwg.add(dwg.rect(insert=(0, 0), size=("100%", "100%"), class_="background"))

        # Render grid
        self.__render_grid()

        # Render builds
        self.__render_builds()

        # Save
        dwg.save(pretty=True)

    def print_svg_to_tmp(self):

        # First print as svg in a temporary file
        f = tempfile.NamedTemporaryFile(delete=True)
        self.print_svg(f.name)

        return f

    def print_png(self, output):

        # First print as svg in a temporary file
        f = self.print_svg_to_tmp()

        # Convert that file from svg to png
        cairosvg.svg2png(url=f.name, write_to=output)

        # Remove temporary file
        f.close()

    def print_html(self, output):

        # Configure rendering
        self.show_time = False

        # First print as svg in a temporary file
        svg_content = None
        with self.print_svg_to_tmp() as f_svg:
            svg_content = f_svg.read()

        title = "%s #%s" % (self.job_info.job_name, self.job_info.build_number)

        img_src = "data:image/svg+xml;base64,%s" % u"".join(
            base64.encodebytes(svg_content).decode("utf-8").splitlines()
        )

        map_content = []
        tooltips_content = []
        for build_r in self.rect_builds.values():
            build = build_r["build"]
            link = build.build_url()
            tooltip_id = "tooltip-%s-%s" % (build.job_name, build.build_number)
            tooltip_id = tooltip_id.replace(".", "_")
            area = (
                '<area shape="rect" coords="%d,%d,%d,%d" href="%s" data-tooltip="%s"/>'
                % (
                    build_r["insert"][0],
                    build_r["insert"][1],
                    build_r["insert"][0] + build_r["size"][0],
                    build_r["insert"][1] + build_r["size"][1],
                    link,
                    "#" + tooltip_id,
                )
            )
            map_content.append(area)

            queue_time = get_human_time(build.queueing_duration)
            exec_time = get_human_time(build.duration)
            tooltip_lines = []
            tooltip_lines.append("<b>Queue Time:</b> %s<br/>" % queue_time)
            tooltip_lines.append("<b>Exec Time:</b> %s<br/>" % exec_time)
            tooltip_lines.append("<b>Result:</b> %s<br/>" % build.result)
            if build.description:
                tooltip_lines.append(
                    '<b>Description:</b><br/><div class="description">%s</div>'
                    % (build.description)
                )

            if len(build.failure_causes) != 0:
                tooltip_lines.append("<b>Failure Causes:</b><br/>")
                for cause in build.failure_causes:
                    tooltip_lines.append(
                        "- <em>%s:</em><br/>" % html.escape(cause["name"])
                    )
                    if cause.get("description"):
                        tooltip_lines.append(
                            '<p class="failure-cause">%s</p>'
                            % html.escape(cause["description"])
                        )

            if build.sections and len(build.sections) != 0:
                tooltip_lines.append("<b>Sections:</b><br/>")
                tooltip_lines.append('<div class="sections">')
                for section in build.sections:
                    padding = "&nbsp;" * section.parents_cnt * 2
                    txt = section
                    if section.end is None:
                        txt = '<span class="error">%s</span>' % section
                    tooltip_lines.append("%s⊩ %s<br/>" % (padding, txt))
                tooltip_lines.append("</div>")

            tooltip = '<div class="tooltip" id="%s">%s</div>' % (
                tooltip_id,
                "\n".join(tooltip_lines),
            )
            tooltips_content.append(tooltip)

        head_content = ""
        if self.show_infobox:
            head_content += MAPHIGHLIGHT_SCRIPT

        with open(output, "w") as f_html:
            html_content = HTML_TMPL % (
                title,
                head_content,
                self.result,
                img_src,
                "\n".join(map_content),
                "\n".join(tooltips_content),
            )
            f_html.write(html_content)

    @property
    def result(self):
        key = next(iter(self.rect_builds))
        return self.rect_builds[key]["build"].result

    def print(self, output):
        logger.debug("Output to %s", output)

        if output.endswith(".svg"):
            self.print_svg(output)
        elif output.endswith(".png"):
            self.print_png(output)
        elif output.endswith(".html") or output.endswith(".htm"):
            self.print_html(output)
        else:
            raise Exception("Format not supported")
