# @file Edk2CiBuild.py
# This module contains code that supports CI/CD
# This is the main entry for the build and test process
# of Non-Product builds
##
# Copyright (c) Microsoft Corporation
#
# SPDX-License-Identifier: BSD-2-Clause-Patent
##
"""Code that supports CI/CD via the ci_build invocable.

Contains a CIBuildSettingsManager that must be subclassed in a build settings
file. This provides platform specific information to Edk2CiBuild invocable
while allowing the invocable itself to remain platform agnostic.
"""
import os
import sys
import logging
import yaml
import traceback
from typing import Dict, Any
from edk2toollib.uefi.edk2.path_utilities import Edk2Path
from edk2toollib.log.junit_report_format import JunitTestReport
from edk2toolext.invocables.edk2_multipkg_aware_invocable import Edk2MultiPkgAwareInvocable
from edk2toolext.invocables.edk2_multipkg_aware_invocable import MultiPkgAwareSettingsInterface
from edk2toolext.environment import self_describing_environment
from edk2toolext.environment.plugintypes.ci_build_plugin import ICiBuildPlugin
from edk2toolext.environment import shell_environment
from edk2toolext import edk2_logging


class CiBuildSettingsManager(MultiPkgAwareSettingsInterface):
    """Platform specific settings for Edk2CiBuild.

    Provide information necessary for `stuart_ci_build.exe` or
    `edk2_ci_build.py` to successfully execute.

    !!! example "Example of Overriding CiBuildSettingsManager"
        ```python
        from edk2toolext.invocables.edk2_ci_build import CiBuildSettingsManager
        import yaml
        class CiManager(CiBuildSettingsManager):
            def GetDependencies(self):
                return {
                    "Path": "/Common/MU",
                    "Url":  "https://github.com/Microsoft/mu_tiano_plus.git"
                }
        ```
    """

    def GetName(self) -> str:
        """Get the name of the repo, platform, or product being build by CI.

        !!! tip
            Required Override in a subclass

        Returns:
            (str): repo, platform, product
        """
        raise NotImplementedError()

    def GetPluginSettings(self) -> Dict[str, Any]:
        """Provide a dictionary of global settings for individual plugins.

        !!! tip
            Optional Override in a subclass

        !!! warning
            This sets the global plugin configurations. Edk2CiBuild automatically searches for,
            and loads, the package ci settings file if it exists. This file will override these
            settings. This file must be located at the base of the package named [Package].ci.yaml.

            Ex: EmbeddedPkg/EmbeddedPkg.ci.yaml.

        Returns:
            (Dict[str, Any]): plugin settings
        """
        return {}


class Edk2CiBuild(Edk2MultiPkgAwareInvocable):
    """Invocable supporting an iterative multi-package build and test process leveraging CI build plugins."""

    def GetSettingsClass(self):
        """Returns the CiBuildSettingsManager class.

        !!! warning
            CiBuildSettingsManager must be subclassed in your platform settings file.
        """
        return CiBuildSettingsManager

    def GetLoggingFileName(self, loggerType):
        """Returns the filename (CI_BUILDLOG) of where the logs for the Edk2CiBuild invocable are stored in."""
        return "CI_BUILDLOG"

    def Go(self):
        """Executes the core functionality of the Edk2CiBuild invocable."""
        log_directory = os.path.join(self.GetWorkspaceRoot(), self.GetLoggingFolderRelativeToRoot())

        Edk2CiBuild.collect_python_pip_info()

        # make Edk2Path object to handle all path operations
        try:
            edk2path = Edk2Path(self.GetWorkspaceRoot(), self.PlatformSettings.GetPackagesPath())
        except Exception as e:
            logging.error("Src Tree is invalid.  Did you Setup correctly?")
            raise e

        logging.info(f"Running CI Build: {self.PlatformSettings.GetName()}")
        logging.info(f"WorkSpace: {edk2path.WorkspacePath}")
        logging.info(f"Package Path: {os.pathsep.join(edk2path.PackagePathList)}")
        # Bring up the common minimum environment.
        logging.log(edk2_logging.SECTION, "Getting Environment")
        (build_env, shell_env) = self_describing_environment.BootstrapEnvironment(
            self.GetWorkspaceRoot(), self.GetActiveScopes(), self.GetSkippedDirectories())
        env = shell_environment.GetBuildVars()

        # Bind our current execution environment into the shell vars.
        ph = os.path.dirname(sys.executable)
        if " " in ph:
            ph = '"' + ph + '"'
        shell_env.set_shell_var("PYTHON_HOME", ph)
        # PYTHON_COMMAND is required to be set for using edk2 python builds.
        # todo: work with edk2 to remove the bat file and move to native python calls.
        #       This would be better in an edk2 plugin so that it could be modified/controlled
        #       more easily
        #
        pc = sys.executable
        if " " in pc:
            pc = '"' + pc + '"'
        shell_env.set_shell_var("PYTHON_COMMAND", pc)

        env.SetValue("TARGET_ARCH", " ".join(self.requested_architecture_list), "from edk2 ci build.py")

        # Generate consumable XML object- junit format
        JunitReport = JunitTestReport()

        # Keep track of failures
        failure_num = 0
        total_num = 0

        # Load plugins
        logging.log(edk2_logging.SECTION, "Loading plugins")

        pluginList = self.plugin_manager.GetPluginsOfClass(ICiBuildPlugin)

        for pkgToRunOn in self.requested_package_list:
            #
            # run all loaded Edk2CiBuild Plugins/Tests
            #
            logging.log(edk2_logging.SECTION, f"Building {pkgToRunOn} Package")
            logging.info(f"Running on Package: {pkgToRunOn}")
            package_class_name = f"Edk2CiBuild.{self.PlatformSettings.GetName()}.{pkgToRunOn}"
            ts = JunitReport.create_new_testsuite(pkgToRunOn, package_class_name)
            packagebuildlog_path = os.path.join(log_directory, pkgToRunOn)
            _, txt_handle = edk2_logging.setup_txt_logger(
                packagebuildlog_path, f"BUILDLOG_{pkgToRunOn}", logging_level=logging.DEBUG, isVerbose=True)
            loghandle = [txt_handle]
            shell_environment.CheckpointBuildVars()
            env = shell_environment.GetBuildVars()

            # load the package level .ci.yaml
            pkg_config_file = edk2path.GetAbsolutePathOnThisSystemFromEdk2RelativePath(
                os.path.join(pkgToRunOn, pkgToRunOn + ".ci.yaml"))
            if (pkg_config_file):
                with open(pkg_config_file, 'r') as f:
                    pkg_config = yaml.safe_load(f)
            else:
                logging.info(f"No Pkg Config file for {pkgToRunOn}")
                pkg_config = dict()

            # get all the defines from the package configuration
            if "Defines" in pkg_config:
                for definition_key in pkg_config["Defines"]:
                    definition = pkg_config["Defines"][definition_key]
                    env.SetValue(definition_key, definition, "Edk2CiBuild.py from PkgConfig yaml", False)

            # For each plugin
            for Descriptor in pluginList:
                # For each target
                for target in self.requested_target_list:

                    if (target not in Descriptor.Obj.RunsOnTargetList()):
                        continue

                    edk2_logging.log_progress(f"--Running {pkgToRunOn}: {Descriptor.Name} {target} --")
                    total_num += 1
                    shell_environment.CheckpointBuildVars()
                    env = shell_environment.GetBuildVars()

                    env.SetValue("TARGET", target, "Edk2CiBuild.py before RunBuildPlugin")
                    (testcasename, testclassname) = Descriptor.Obj.GetTestName(package_class_name, env)
                    tc = ts.create_new_testcase(testcasename, testclassname)

                    # create the stream for the build log
                    plugin_output_stream = edk2_logging.create_output_stream()

                    # merge the repo level and package level for this specific plugin
                    pkg_plugin_configuration = Edk2CiBuild.merge_config(self.PlatformSettings.GetPluginSettings(),
                                                                        pkg_config, Descriptor.descriptor)

                    # Still need to see if the package decided this should be skipped
                    if pkg_plugin_configuration is None or\
                            "skip" in pkg_plugin_configuration and pkg_plugin_configuration["skip"]:
                        tc.SetSkipped()
                        edk2_logging.log_progress("--->Test Skipped by package! %s" % Descriptor.Name)

                    else:
                        try:
                            #   - package is the edk2 path to package.  This means workspace/package path relative.
                            #   - edk2path object configured with workspace and packages path
                            #   - any additional command line args
                            #   - RepoConfig Object (dict) for the build
                            #   - PkgConfig Object (dict)
                            #   - EnvConfig Object
                            #   - Plugin Manager Instance
                            #   - Plugin Helper Obj Instance
                            #   - testcase Object used for outputing junit results
                            #   - output_stream the StringIO output stream from this plugin
                            rc = Descriptor.Obj.RunBuildPlugin(pkgToRunOn, edk2path, pkg_plugin_configuration,
                                                               env, self.plugin_manager, self.helper,
                                                               tc, plugin_output_stream)
                        except Exception as exp:
                            exc_type, exc_value, exc_traceback = sys.exc_info()
                            logging.critical("EXCEPTION: {0}".format(exp))
                            exceptionPrint = traceback.format_exception(type(exp), exp, exc_traceback)
                            logging.critical(" ".join(exceptionPrint))
                            tc.SetError("Exception: {0}".format(
                                exp), "UNEXPECTED EXCEPTION")
                            rc = 1

                        if (rc > 0):
                            failure_num += 1
                            if (rc is None):
                                logging.error(
                                    f"--->Test Failed: {Descriptor.Name} {target} returned NoneType")
                            else:
                                logging.error(
                                    f"--->Test Failed: {Descriptor.Name} {target} returned {rc}")
                        elif (rc < 0):
                            logging.warn(f"--->Test Skipped: in plugin! {Descriptor.Name} {target}")
                        else:
                            edk2_logging.log_progress(f"--->Test Success: {Descriptor.Name} {target}")

                    # revert to the checkpoint we created previously
                    shell_environment.RevertBuildVars()
                    # remove the logger
                    edk2_logging.remove_output_stream(plugin_output_stream)
                # finished target loop
            # Finished plugin loop

            edk2_logging.stop_logging(loghandle)  # stop the logging for this particular buildfile
            shell_environment.RevertBuildVars()
        # Finished buildable file loop

        JunitReport.Output(os.path.join(self.GetWorkspaceRoot(), "Build", "TestSuites.xml"))

        # Print Overall Success
        if (failure_num != 0):
            logging.error("Overall Build Status: Error")
            edk2_logging.log_progress(f"There were {failure_num} failures out of {total_num} attempts")
        else:
            edk2_logging.log_progress("Overall Build Status: Success")

        return failure_num

    @staticmethod
    def merge_config(gbl_config, pkg_config, descriptor={}):
        """Merge two configurations.

        One global and one specificto the package to create the proper config
        for a plugin to execute.

        Returns:
            (dict): Dictionary of config settings
        """
        plugin_name = ""
        config = dict()
        if "module" in descriptor:
            plugin_name = descriptor["module"]
        if "config_name" in descriptor:
            plugin_name = descriptor["config_name"]

        if plugin_name == "":
            return config

        if plugin_name in gbl_config:
            config.update(gbl_config[plugin_name])

        if plugin_name in pkg_config:
            config.update(pkg_config[plugin_name])

        return config


def main():
    """Entry point to invoke Edk2CiBuild."""
    Edk2CiBuild().Invoke()
