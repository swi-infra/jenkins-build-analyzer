import svgwrite
import logging
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

      rect.aborted       { fill: rgb(190,200,183); fill-opacity: 0.7; stroke: rgb(190,200,183); stroke-width: 2; stroke-opacity: 1.0; }
      rect.success       { fill: rgb(170,255,170); fill-opacity: 0.7; stroke: rgb(170,255,170); stroke-width: 2; stroke-opacity: 1.0; }
      rect.infra_failure { fill: rgb(249,170,255); fill-opacity: 0.7; stroke: rgb(249,170,255); stroke-width: 2; stroke-opacity: 1.0; }
      rect.failure       { fill: rgb(255,170,170); fill-opacity: 0.7; stroke: rgb(255,170,170); stroke-width: 2; stroke-opacity: 1.0; }
      rect.unstable      { fill: rgb(255,204,170); fill-opacity: 0.7; stroke: rgb(255,204,170); stroke-width: 2; stroke-opacity: 1.0; }
      rect.other         { fill: rgb(204,204,204); fill-opacity: 0.7; stroke: rgb(204,204,204); stroke-width: 2; stroke-opacity: 1.0; }
      rect.in_progress   { fill: rgb(135,205,222); fill-opacity: 0.7; stroke: rgb(135,205,222); stroke-width: 2; stroke-opacity: 1.0; }

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
        if($(tooltip).width() < 300)
        {
            $(tooltip).css('width', 300);
        }
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


class BoundaryBox:
    def __init__(self, obj, x=None, y=None, max_x=None, max_y=None):
        self.obj = obj
        self.x = x
        self.y = y
        self.max_x = max_x
        self.max_y = max_y

    def __str__(self):
        return "Box %s: (x=%s, y=%s) -> (x=%s, y=%s)" % (
            self.obj,
            self.x,
            self.y,
            self.max_x,
            self.max_y,
        )

    def add_rect(self, insert, size):
        max_x = insert[0] + size[0]
        max_y = insert[1] + size[1]
        # logger.debug("Add rect x1%s width%s -> x2%s", insert, size, (max_x, max_y))
        if self.x is None or insert[0] < self.x:
            self.x = insert[0]
        if self.y is None or insert[1] < self.y:
            self.y = insert[1]
        if self.max_x is None or self.max_x < max_x:
            self.max_x = max_x
        if self.max_y is None or self.max_y < max_y:
            self.max_y = max_y

    def add_text(self, text, insert, class_):
        font_size = 14
        if class_ == "min":
            font_size = 10
        if class_ == "time":
            font_size = 5
        width = len(text) * (font_size * 0.65)
        height = font_size
        size = (width, height)
        self.add_rect(insert, size)
        return size


class SvgPrinter:
    def __init__(self, job_info):
        self.job_info = job_info

        self.margin = 20
        self.extra_width = 200

        # Options
        self.show_build_name = True
        self.show_queue = True
        self.show_time = False
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
        self.index_mode = "stairs"

        self.__dwg = None
        self.current_pos = None

        self.all_builds = self.job_info.all_builds
        self.build_result = self.job_info.result
        self.duration = self.job_info.duration
        self.max_duration = 0

        self.boundary_boxes = {}
        self.lanes = {}

    def __determine_sizes(self):

        self.base_timestamp = self.job_info.start

        # First render pass to determine the sizes
        self.__render_builds(render=False)

        # Height based on number of lanes
        self.box_height = self.build_height * len(self.lanes)
        self.total_height = 2 * self.margin + self.box_height

        # Width based on largest lane
        max_x = 0
        for lane in self.lanes:
            if self.lanes[lane][-1].max_x > max_x:
                max_x = self.lanes[lane][-1].max_x
        self.max_duration = int(max_x / self.minute_width)

        self.box_width = max_x
        if self.box_width < self.min_width:
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

    def __render_section(self, build, section, build_index, boundary_box, render):
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

        if render:
            dwg.add(
                dwg.rect(
                    insert=(x, y + self.build_height - self.build_padding),
                    size=(duration_px, (1 + section_index) * self.section_height),
                    class_=class_name,
                )
            )

    def __determine_index(self, build, index, boundary_box, x):

        if build.lane_index is not None:
            index = build.lane_index
        else:
            index = self.__determine_next_lane(index, x)
        if index not in self.lanes:
            self.lanes[index] = []
        self.lanes[index].append(boundary_box)

        return index

    def __render_queue(self, build, build_index, boundary_box, render):
        dwg = self.__dwg

        if not self.show_queue:
            return None
        if build.queueing_duration is None:
            return None

        offset = (
            (build.start - build.queueing_duration - self.base_timestamp) / 1000 / 60
        )
        offset_px = offset * self.minute_width

        duration = build.queueing_duration / 1000 / 60
        duration_px = duration * self.minute_width

        class_name = "queue"

        x = self.margin + offset_px

        build_index = self.__determine_index(build, build_index, boundary_box, x)

        y = self.margin + build_index * self.build_height

        insert = (x, y + self.build_padding)
        size = (duration_px, self.build_height - 2 * self.build_padding)

        boundary_box.add_rect(insert=insert, size=size)
        if render:
            dwg.add(
                dwg.rect(
                    insert=insert,
                    size=size,
                    class_=class_name,
                )
            )

        return build_index

    def __render_build(self, build, index, boundary_box=None, render=True):
        dwg = self.__dwg

        offset = 0
        if build.start:
            offset = (build.start - self.base_timestamp) / 1000 / 60
        offset_px = offset * self.minute_width

        x = self.margin + offset_px

        logger.debug("Rendering build %s in lane %d (x=%s)", build, index, x)

        queue_index = self.__render_queue(build, index, boundary_box, render)
        if queue_index is None:
            index = self.__determine_index(build, index, boundary_box, x)
        else:
            index = queue_index

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

        y = self.margin + index * self.build_height

        build_id = "%s#%s" % (build.job_name, build.build_number)

        build_r = {
            "build": build,
            "insert": (x, y + self.build_padding),
            "size": (duration_px, self.build_height - 2 * self.build_padding),
        }
        self.rect_builds[build_id] = build_r
        boundary_box.add_rect(insert=build_r["insert"], size=build_r["size"])
        if render:
            dwg.add(
                dwg.rect(
                    insert=build_r["insert"], size=build_r["size"], class_=class_name
                )
            )

        if build.sections:
            for section in build.sections:
                self.__render_section(build, section, index, boundary_box, render)

        if self.show_build_name:
            build_info = ""
            if build.stage:
                build_info = "[%s] " % build.stage
            build_info += build_id

            text_pos = (x + 5, y + self.build_height - self.build_padding - 8)
            boundary_box.add_text(build_info, insert=text_pos, class_="min")
            if render:
                dwg.add(
                    dwg.text(
                        build_info,
                        insert=text_pos,
                        class_="min",
                    )
                )

        if self.show_time:
            queue_time = get_human_time(build.queueing_duration)
            exec_time = get_human_time(build.duration)
            build_time = "[queue: %s; build: %s]" % (queue_time, exec_time)

            text_pos = (x + 5, y + self.build_height - self.build_padding - 1)
            boundary_box.add_text(build_time, insert=text_pos, class_="time")
            if render:
                dwg.add(
                    dwg.text(
                        build_time,
                        insert=text_pos,
                        class_="time",
                    )
                )

        return index

    def __determine_next_lane(self, index, x):
        next_index = None

        if self.index_mode == "stairs":
            if index is None:
                next_index = 0
            else:
                next_index = index + 1

        if self.index_mode == "compact":
            max_lane = None

            for lane in self.lanes:
                boundary_box = None
                if len(self.lanes[lane]) != 0:
                    boundary_box = self.lanes[lane][-1]
                if boundary_box is None:
                    # Lane is empty, select it
                    max_lane = lane
                    break
                if x > boundary_box.max_x:
                    # The start of our position is above the max of the last box on that lane, select it
                    max_lane = lane
                    break
            else:
                # If there is no space in the existing lanes, add a new one
                max_lane = len(self.lanes)

            next_index = max_lane

        if next_index is None:
            raise Exception("Unknown index mode %s" % self.index_mode)

        logger.debug("Next index: Index %s w/ x=%s => Next %s", index, x, next_index)
        return next_index

    def __render_builds(self, render=True):
        index = None
        self.lanes = {}
        self.boundary_boxes = {}

        def sort_build(build):
            if build.lane_index is not None:
                return build.lane_index
            return build.start

        all_builds = self.all_builds
        if self.index_mode != "stairs":
            all_builds = sorted(self.all_builds, key=sort_build)

        for build in all_builds:
            boundary_box = BoundaryBox(build)
            self.boundary_boxes[build] = boundary_box
            index = self.__render_build(build, index, boundary_box, render)

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
            if not self.show_build_name:
                tooltip_lines.append("<b>Build:</b> %s<br/>" % build)
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
