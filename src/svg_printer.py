
import svgwrite
import coloredlogs, logging
import math

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

      rect.aborted      { fill: rgb(190,200,183); fill-opacity: 0.7; }
      rect.success      { fill: rgb(170,255,170); fill-opacity: 0.7; }
      rect.failure      { fill: rgb(255,170,170); fill-opacity: 0.7; }
      rect.unstable     { fill: rgb(255,204,170); fill-opacity: 0.7; }
      rect.other        { fill: rgb(204,204,204); fill-opacity: 0.7; }
      rect.in_progress  { fill: rgb(135,205,222); fill-opacity: 0.7; }

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

      text       { font-family: Verdana, Helvetica; font-size: 14px; }
      text.left  { font-family: Verdana, Helvetica; font-size: 14px; text-anchor: start; }
      text.right { font-family: Verdana, Helvetica; font-size: 14px; text-anchor: end; }
      text.min   { font-size: 10px; }
"""

class SvgPrinter:

    def __init__(self, job_info):
        self.job_info = job_info

        self.margin = 20
        self.extra_width = 200

        self.build_padding = 5
        self.build_height = 30
        self.section_height = 2
        self.minute_width = 10

        self.__dwg = None

    def __determine_sizes(self):

        self.base_timestamp = self.job_info.start()

        # Height based on number of builds to show
        self.box_height = self.build_height*len(self.job_info.all_builds())
        self.total_height = 2*self.margin + self.box_height

        # Width based on duration
        self.max_duration = self.job_info.duration()
        if self.job_info.result() == "IN_PROGRESS":
            for build in self.job_info.all_builds():
                self.max_duration = max(self.max_duration, build.start() + build.duration() - self.base_timestamp)

        self.max_duration = math.ceil(self.max_duration / 1000 / 60 / 5) * 5 # in minutes, rounded up

        self.box_width = self.minute_width*self.max_duration
        self.total_width = 2*self.margin + self.box_width + self.extra_width

        logger.debug("Total: %d x %d" % (self.total_height, self.total_width))

    def __render_grid(self):
        dwg = self.__dwg

        self.current_pos = self.margin

        dwg.add(dwg.rect(insert=(self.margin, self.margin),
                         size=(self.box_width, self.box_height),
                         class_='box'))

        for x in range(0, self.max_duration):

            class_name = "min01"
            if x % 5 == 0:
                class_name = "min5"
                if x % 60 == 0:
                    class_name = "min60"

                dwg.add(dwg.text("%dmin" % x,
                                 insert=(self.current_pos, self.margin-5),
                                 class_="min"))

            dwg.add(dwg.line(start=(self.current_pos, self.margin),
                             end=(self.current_pos, self.total_height-self.margin),
                             class_=class_name))

            self.current_pos += self.minute_width

    def __render_section(self, section, build_index):
        dwg = self.__dwg

        offset = (section.start - self.base_timestamp) / 1000 / 60
        if offset < 0:
            offset = 0
        offset_px = offset * self.minute_width

        duration = section.duration() / 1000 / 60
        if not section.end:
            duration = self.max_duration - offset
        duration_px = duration * self.minute_width

        class_name = "type"
        if section.type():
            class_name = "type_%s" % section.type()

        section_index = section.parents_cnt()
        if section_index >= 4:
          section_index = 4

        x = self.margin + offset_px
        y = self.margin + build_index*self.build_height

        dwg.add(dwg.rect(insert=(x, y + self.build_height - self.build_padding),
                         size=(duration_px, (1 + section_index)*self.section_height),
                         class_=class_name))

    def __render_queue(self, build, build_index):
        dwg = self.__dwg

        if build.queueing_duration() == 0:
            return

        offset = (build.start() - build.queueing_duration() - self.base_timestamp) / 1000 / 60
        offset_px = offset * self.minute_width

        duration = build.queueing_duration() / 1000 / 60
        duration_px = duration * self.minute_width

        class_name = "queue"

        x = self.margin + offset_px
        y = self.margin + build_index*self.build_height

        dwg.add(dwg.rect(insert=(x, y + self.build_padding),
                         size=(duration_px, self.build_height - 2*self.build_padding),
                         class_=class_name))

    def __render_build(self, build, index):
        dwg = self.__dwg

        self.__render_queue(build, index)

        offset = (build.start() - self.base_timestamp)  / 1000 / 60
        offset_px = offset * self.minute_width

        duration = build.duration() / 1000 / 60
        if build.result() == "IN_PROGRESS":
            duration = self.max_duration - offset
        duration_px = duration * self.minute_width

        class_name = 'other'
        if build.result() == "SUCCESS":
            class_name = 'success'
        elif build.result() == "ABORTED":
            class_name = 'aborted'
        elif build.result() == "FAILURE":
            class_name = 'failure'
        elif build.result() == "UNSTABLE":
            class_name = 'unstable'
        elif build.result() == "IN_PROGRESS":
            class_name = 'in_progress'

        if build.job_type() == 'pipeline' or \
           build.job_type() == 'buildFlow':
          class_name = "pipe_%s" % class_name

        x = self.margin + offset_px
        y = self.margin + index*self.build_height

        dwg.add(dwg.rect(insert=(x, y + self.build_padding),
                         size=(duration_px, self.build_height - 2*self.build_padding),
                         class_=class_name))

        for section in build.sections():
            self.__render_section(section, index)

        build_info = ""
        if build.stage:
            build_info = "[%s] " % build.stage
        build_info += "%s#%s" % (build.job_name, build.build_number)
        dwg.add(dwg.text(build_info,
                         insert=(x + 5, y + self.build_height - self.build_padding - 6),
                         class_="min"))

    def __render_builds(self):
        current_idx = 0
        for build in self.job_info.all_builds():
            self.__render_build(build, current_idx)
            current_idx += 1

    def print(self, output):
        print("Output to %s" % output)

        self.__determine_sizes()

        self.__dwg = svgwrite.Drawing(filename=output,
                                      size=(self.total_width, self.total_height),
                                      debug=True)

        dwg = self.__dwg

        # Add styles
        dwg.defs.add(dwg.style(STYLES))

        # Background
        dwg.add(dwg.rect(insert=(0, 0),
                         size=('100%', '100%'),
                         class_='background'))

        # Render grid
        self.__render_grid()

        # Render builds
        self.__render_builds()

        # Save
        dwg.save(pretty=True)

