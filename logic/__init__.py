import logging, asyncio, utils
from cxone_api.util import page_generator, json_on_ok
from cxone_api.high.projects import ProjectRepoConfig
from cxone_api.high.access_mgmt.user_mgmt import Groups
from cxone_api.low.projects import retrieve_list_of_projects
from cxone_api.low.iam import retrieve_groups

class Scheduler:
    __log = logging.getLogger("Scheduler")

    async def __get_schedule_entry_from_tag(self, project_data, schedule_tag_value, bad_cb):
        if schedule_tag_value is None or len(schedule_tag_value) == 0:
            if bad_cb is not None:
                bad_cb(project_data['id'], "No schedule tag value.")
            return None
        
        repo_details = await ProjectRepoConfig.from_project_json(self.__client, project_data)
        
        elements = schedule_tag_value.split(":")

        if len(elements) > 0:
            ss = utils.ScheduleString(elements.pop(0), self.__policies)

            if ss.is_valid():
                branch = elements.pop(0) if len(elements) > 0 else ''
                if len(branch) == 0:
                    branch = await repo_details.primary_branch
                
                if branch is None:
                    if bad_cb is not None:
                        bad_cb(project_data['id'], "Scan branch can't be determined.")
                    return None
                
                engines_from_tag = elements.pop(0) if len(elements) > 0 else None
                engines = None

                if engines_from_tag is None:
                    engines = await repo_details.get_enabled_scanners(branch)

                if engines is None or len(engines) == 0:
                    engines = utils.normalize_selected_engines_from_tag(engines_from_tag if engines_from_tag is not None else 'all')

                if engines is None:
                    if bad_cb is not None:
                        bad_cb(project_data['id'], "Scan engines can't be determined.")
                    return None

                repo_url = await repo_details.repo_url
                if repo_url is None:
                    if bad_cb is not None:
                        bad_cb(project_data['id'], "Repository URL is not set.")
                    return None

                if repo_url is not None and branch is not None:
                    return {project_data['id'] : [utils.ProjectSchedule(project_data['id'], ss, branch, engines, await repo_details.repo_url)]}
            else:
                Scheduler.__log.error(f"Project {project_data['id']}:{project_data['name']} has invalid schedule tag {schedule_tag_value}, skipping.")
                if bad_cb is not None:
                    bad_cb(project_data['id'], f"Bad schedule tag value.")

        return None

    async def __get_tagged_project_schedule(self, bad_cb):
        Scheduler.__log.debug("Begin: Load tagged project schedule")
        schedules = {}
        async for tagged_project in page_generator(retrieve_list_of_projects, "projects", client=self.__client,tags_keys="schedule"):
            entry = await self.__get_schedule_entry_from_tag(tagged_project, tagged_project['tags']['schedule'], bad_cb)
            if entry is not None:
                schedules.update(entry)
            else:
                Scheduler.__log.debug(f"NO SCHEDULE ENTRY: {tagged_project}")

        Scheduler.__log.debug("End: Load tagged project schedule")
        return schedules

    async def __get_untagged_project_schedule(self, bad_cb):
        result = {}

        if not self.__group_schedules.empty or self.__default_schedule is not None:
            Scheduler.__log.debug("Begin: Load untagged project schedule")

            group_index = Groups(self.__client)

            async for project in page_generator(retrieve_list_of_projects, "projects", client=self.__client):
                project_schedules = []

                # The schedule tag takes precedence
                if "schedule" in project['tags'].keys():
                    continue

                # Check that repo is defined and primary branch is defined
                repo_cfg = await ProjectRepoConfig.from_project_json(self.__client, project)
                if (await repo_cfg.repo_url is not None) and (await repo_cfg.primary_branch is not None):
                    # If the project matches a group, assign it the schedule for all matching groups.
                    for gid in project['groups']:
                        g_desc = await group_index.get_by_id(gid)

                        if g_desc is not None:
                            ss = self.__group_schedules.get_schedule(str(g_desc.path))
                        
                            if ss is not None:
                                project_schedules.append(utils.ProjectSchedule(project['id'], ss, 
                                                                            await repo_cfg.primary_branch, utils.normalize_selected_engines_from_tag('all'), await repo_cfg.repo_url))

                    if len(project_schedules) > 0:
                        result[project['id']] = project_schedules
                    elif self.__default_schedule is not None:
                        ss = utils.ScheduleString(self.__default_schedule, self.__policies)
                        if ss.is_valid():
                            result[project['id']] = [utils.ProjectSchedule(project['id'], ss.get_crontab_schedule(), 
                                                                                await repo_cfg.primary_branch, utils.normalize_selected_engines_from_tag('all'), await repo_cfg.repo_url)]
                else:
                    if self.__default_schedule is not None and bad_cb is not None:
                        bad_cb(project['id'], f"Project [{project['name']}] has a misconfigured repo url or primary branch.")

            Scheduler.__log.debug("End: Load untagged project schedule")
        else:
            Scheduler.__log.debug("No untagged schedules loaded")

        return result

    async def __get_changed_projects(self, new_schedule):
        check_projects = set(new_schedule.keys()) & set(self.__the_schedule.keys())

        result = {}
        for k in check_projects:
            if not len(new_schedule[k]) == len(self.__the_schedule[k]):
                result[k] = new_schedule[k]
            else:
                new_schedule_comps = set([str(x) for x in new_schedule[k]])
                old_schedule_comps = set([str(x) for x in self.__the_schedule[k]])
                if len(new_schedule_comps - old_schedule_comps) > 0:
                    result[k] = new_schedule[k]

        return result

    async def refresh_schedule(self):
        new_schedule = await self.__load_schedule()

        # Schedules that are in new_schedule but not in the current schedule are new
        # and can be written immediately.
        new_scheduled_projects = set(new_schedule.keys()) - set(self.__the_schedule.keys())
        new_schedules = {k:new_schedule[k] for k in new_scheduled_projects}
        self.__log.debug(f"Writing {len(new_scheduled_projects)} new project schedules")
        utils.write_schedule(new_schedules)
        
        # Schedules that are in the current schedule but not in the new schedule can
        # be removed.
        removed_projects = set(self.__the_schedule.keys()) - set(new_schedule.keys())
        removed_schedules = {k:self.__the_schedule[k] for k in removed_projects}
        self.__log.debug(f"Deleting {len(removed_projects)} project schedules")
        utils.delete_scheduled_projects(removed_schedules)
        for removed in removed_projects:
            self.__the_schedule.pop(removed, None)

        # Schedules that still exist should be checked for changes.  Any changed
        # schedules need to be re-written.
        changed_schedule = await self.__get_changed_projects(new_schedule)
        self.__log.debug(f"Changing {len(changed_schedule)} project schedules")
        utils.delete_scheduled_projects(changed_schedule)
        utils.write_schedule(changed_schedule)
        for k in changed_schedule.keys():
            self.__the_schedule.pop(k, None)
            self.__the_schedule[k] = changed_schedule[k]


        self.__the_schedule = self.__the_schedule | new_schedules

        return len(new_scheduled_projects), len(removed_projects), len(changed_schedule.keys())

    @property
    def scheduled_scans(self):
        return len(self.__the_schedule.keys())
        

    async def __load_schedule(self, bad_cb = None):
        tagged, grouped = await asyncio.gather(self.__get_tagged_project_schedule(bad_cb), self.__get_untagged_project_schedule(bad_cb))

        # It is possible that modifications were done to projects while compiling schedules.  If there are intersections,
        # the tagged project takes precedence.
        intersection = list(set(tagged.keys()) & set(grouped.keys()) )

        for k in intersection:
            grouped.pop(k, None)


        return tagged | grouped


    @staticmethod
    async def __initialize(client, default_schedule, group_schedules, policies):
        ret_sched = Scheduler()
        ret_sched.__client = client
        ret_sched.__group_schedules = group_schedules
        ret_sched.__policies = policies
        if default_schedule is not None and default_schedule in ret_sched.__policies.keys():
            ret_sched.__default_schedule = default_schedule
        else:
            ret_sched.__default_schedule = None
            Scheduler.__log.error(f"Default schedule [{default_schedule}] is not a valid policy.")

        return ret_sched
    
    @staticmethod
    async def audit(client, default_schedule, group_schedules, policies, bad_callback):
        schedule = await Scheduler.__initialize(client, default_schedule, group_schedules, policies)
        return await schedule.__load_schedule(bad_callback)

    @staticmethod
    async def start(client, default_schedule, group_schedules, policies):

        ret_sched = await Scheduler.__initialize(client, default_schedule, group_schedules, policies)
        ret_sched.__the_schedule = await ret_sched.__load_schedule(None)

        utils.write_schedule(ret_sched.__the_schedule)

        return ret_sched


