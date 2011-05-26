#
# __init__.py
#
# Copyright (C) 2010  Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Red Hat Author(s):  Martin Gracik <mgracik@redhat.com>
#                     David Cantrell <dcantrell@redhat.com>
#                     Will Woods <wwoods@redhat.com>

# set up logging
import logging
logger = logging.getLogger("pylorax")

sh = logging.StreamHandler()
sh.setLevel(logging.INFO)
logger.addHandler(sh)


import sys
import os
import ConfigParser
import tempfile

from base import BaseLoraxClass, DataHolder
import output

import yum
import ltmpl

import imgutils
import constants
from sysutils import *

from treebuilder import RuntimeBuilder, TreeBuilder
from buildstamp import BuildStamp
from treeinfo import TreeInfo
from discinfo import DiscInfo

class ArchData(DataHolder):
    lib64_arches = ("x86_64", "ppc64", "sparc64", "s390x", "ia64")
    archmap = {"i386": "i386", "i586":"i386", "i686":"i386", "x86_64":"x86_64",
               "ppc":"ppc", "ppc64": "ppc",
               "sparc":"sparc", "sparcv9":"sparc", "sparc64":"sparc",
               "s390":"s390", "s390x":"s390x",
    }
    def __init__(self, buildarch):
        self.buildarch = buildarch
        self.basearch = self.archmap.get(buildarch) or buildarch
        self.libdir = "lib64" if buildarch in self.lib64_arches else "lib"

class Lorax(BaseLoraxClass):

    def __init__(self):
        BaseLoraxClass.__init__(self)
        self._configured = False

    def configure(self, conf_file="/etc/lorax/lorax.conf"):
        self.conf = ConfigParser.SafeConfigParser()

        # set defaults
        self.conf.add_section("lorax")
        self.conf.set("lorax", "debug", "1")
        self.conf.set("lorax", "sharedir", "/usr/share/lorax")

        self.conf.add_section("output")
        self.conf.set("output", "colors", "1")
        self.conf.set("output", "encoding", "utf-8")
        self.conf.set("output", "ignorelist", "/usr/share/lorax/ignorelist")

        self.conf.add_section("templates")
        self.conf.set("templates", "ramdisk", "ramdisk.ltmpl")

        self.conf.add_section("yum")
        self.conf.set("yum", "skipbroken", "0")

        self.conf.add_section("compression")
        self.conf.set("compression", "type", "xz")
        self.conf.set("compression", "speed", "9")

        # read the config file
        if os.path.isfile(conf_file):
            self.conf.read(conf_file)

        # set up the output
        debug = self.conf.getboolean("lorax", "debug")
        output_level = output.DEBUG if debug else output.INFO

        colors = self.conf.getboolean("output", "colors")
        encoding = self.conf.get("output", "encoding")

        self.output.basic_config(output_level=output_level,
                                 colors=colors, encoding=encoding)

        ignorelist = self.conf.get("output", "ignorelist")
        if os.path.isfile(ignorelist):
            with open(ignorelist, "r") as fobj:
                for line in fobj:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        self.output.ignore(line)

        # cron does not have sbin in PATH,
        # so we have to add it ourselves
        os.environ["PATH"] = "{0}:/sbin:/usr/sbin".format(os.environ["PATH"])

        self._configured = True

    def init_file_logging(self, logdir, logname="pylorax.log"):
        fh = logging.FileHandler(filename=joinpaths(logdir, logname), mode="w")
        fh.setLevel(logging.DEBUG)
        logger.addHandler(fh)

    def run(self, ybo, product, version, release, variant="", bugurl="",
            is_beta=False, workdir=None, outputdir=None):

        assert self._configured

        # set up work directory
        self.workdir = workdir or tempfile.mkdtemp(prefix="pylorax.work.")
        if not os.path.isdir(self.workdir):
            os.makedirs(self.workdir)

        # set up log directory
        logdir = joinpaths(self.workdir, "log")
        if not os.path.isdir(logdir):
            os.makedirs(logdir)

        self.init_file_logging(logdir)
        logger.debug("using work directory {0.workdir}".format(self))
        logger.debug("using log directory {0}".format(logdir))

        # set up output directory
        self.outputdir = outputdir or tempfile.mkdtemp(prefix="pylorax.out.")
        if not os.path.isdir(self.outputdir):
            os.makedirs(self.outputdir)
        logger.debug("using output directory {0.outputdir}".format(self))

        # do we have root privileges?
        logger.info("checking for root privileges")
        if not os.geteuid() == 0:
            logger.critical("no root privileges")
            sys.exit(1)

        # do we have all lorax required commands?
        self.lcmds = constants.LoraxRequiredCommands()
        # TODO: actually check for required commands (runcmd etc)

        # do we have a proper yum base object?
        logger.info("checking yum base object")
        if not isinstance(ybo, yum.YumBase):
            logger.critical("no yum base object")
            sys.exit(1)

        # create an install root
        self.inroot = joinpaths(ybo.conf.installroot, "installroot")
        if not os.path.isdir(self.inroot):
            os.makedirs(self.inroot)
        logger.debug("using install root: {0}".format(self.inroot))
        ybo.conf.installroot = self.inroot

        logger.info("setting up build architecture")
        self.arch = ArchData(get_buildarch(ybo))
        for attr in ('buildarch', 'basearch', 'libdir'):
            logger.debug("self.arch.%s = %s", attr, getattr(self.arch,attr))

        logger.info("setting up build parameters")
        product = DataHolder(name=product, version=version, release=release,
                             variant=variant, bugurl=bugurl, is_beta=is_beta)
        self.product = product
        logger.debug("product data: %s" % product)

        templatedir = self.conf.get("lorax", "sharedir")
        rb = RuntimeBuilder(self.product, self.arch, ybo, templatedir)

        logger.info("installing runtime packages")
        rb.yum.conf.skip_broken = self.conf.getboolean("yum", "skipbroken")
        rb.install()

        # write .buildstamp
        buildstamp = BuildStamp(self.product.name, self.product.version,
                                self.product.bugurl, self.product.is_beta, self.arch.buildarch)

        buildstamp.write(joinpaths(self.inroot, ".buildstamp"))

        logger.debug("saving pkglists to %s", self.workdir)
        dname = joinpaths(self.workdir, "pkglists")
        os.makedirs(dname)
        for pkgobj in ybo.doPackageLists(pkgnarrow='installed').installed:
            with open(joinpaths(dname, pkgobj.name), "w") as fobj:
                for fname in pkgobj.filelist:
                  fobj.write("{0}\n".format(fname))

        logger.info("doing post-install configuration")
        rb.postinstall() # FIXME: configdir=

        # write .discinfo
        discinfo = DiscInfo(self.product.release, self.arch.basearch)
        discinfo.write(joinpaths(self.outputdir, ".discinfo"))

        logger.info("backing up installroot")
        installroot = joinpaths(self.workdir, "installroot")
        linktree(self.inroot, installroot)

        logger.info("cleaning unneeded files")
        rb.clean()

        logger.info("creating the runtime image")
        # TODO: different img styles / create_runtime implementations
        runtimedir = joinpaths(self.workdir, "runtime")
        # FIXME: compression options (type, speed, etc.)
        rb.create_runtime(runtimedir)

        logger.info("preparing to build output tree and boot images")
        treebuilder = TreeBuilder(self.product, self.arch,
                                  installroot, self.outputdir,
                                  templatedir)

        # TODO: different image styles may do this part differently
        logger.info("rebuilding initramfs images")
        treebuilder.rebuild_initrds(add_args=["--xz"])

        # TODO: keep small initramfs for split initramfs/runtime media?
        logger.info("adding runtime to initrds")
        treebuilder.initrd_append(runtimedir)

        logger.info("populating output tree and building boot images")
        treebuilder.build()

        # write .treeinfo file and we're done
        treeinfo = TreeInfo(self.product.name, self.product.version,
                            self.product.variant, self.arch.basearch)
        for section, data in treebuilder.treeinfo_data.items():
            treeinfo.add_section(section, data)
        treeinfo.write(joinpaths(self.outputdir, ".treeinfo"))

def get_buildarch(ybo):
    # get architecture of the available anaconda package
    available = ybo.doPackageLists(patterns=["anaconda"]).available

    if available:
        anaconda = available.pop(0)
        # src is not a real arch
        if anaconda.arch == "src":
            anaconda = available.pop(0)
        buildarch = anaconda.arch
    else:
        # fallback to the system architecture
        logger.warning("using system architecture")
        buildarch = os.uname()[4]

    return buildarch
