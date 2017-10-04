import logging
import os

from ..utilities.execution_id import gen_execution_id

from ..status import status

from .celery_app import celery_app
from ..specs import pipelines, PipelineSpec, register_all_pipelines
from ..manager.tasks import execute_pipeline

executed_hashes = {}
dependencies = {}
dependents = {}
already_init = False


def collect_dependencies(pipeline_ids):
    if pipeline_ids is None:
        return None
    ret = set()
    for pipeline_id in pipeline_ids:
        ret.add(pipeline_id)
        deps = dependencies.get(pipeline_id)
        if deps is not None:
            ret.update(collect_dependencies(deps))
    return ret


def queue_pipeline(spec: PipelineSpec, trigger):
    ps = status.get(spec.pipeline_id)
    ps.init(spec.pipeline_details,
            spec.source_details,
            spec.validation_errors,
            spec.cache_hash)
    if ps.runnable():
        logging.info('Executing %s task %s', trigger.upper(), spec.pipeline_id)
        eid = gen_execution_id()
        ps.queue_execution(eid, trigger)
        execute_pipeline_task.delay(spec.pipeline_id,
                                    spec.pipeline_details,
                                    spec.path,
                                    trigger,
                                    eid)
        return True
    else:
        logging.warning('Skipping %s task %s, as it has errors %r',
                        trigger.upper(), spec.pipeline_id, spec.validation_errors)
        return False



@celery_app.task
def update_pipelines(action, completed_pipeline_id, completed_trigger):
    # action=init: register all pipelines, trigger anything that's dirty
    # action=update: iterate over all pipelines, register new ones, trigger dirty ones
    # action=complete: iterate over all pipelines, trigger dependencies
    # completed_pipeline_id: pipeline id that had just completed (when applicable)
    # completed_trigger: the trigger for the pipeline that had just completed (when applicable)
    global already_init
    if action == 'init':
        if already_init:
            return
        else:
            register_all_pipelines()
    already_init = True

    logging.debug("Update Pipelines (%s)", action)
    status_all_pipeline_ids = set(status.all_pipeline_ids())
    logging.info("status_all_pipeline_ids %r", status_all_pipeline_ids)
    executed_count = 0
    all_pipeline_ids = set()

    if action == 'complete':
        filter = collect_dependencies(dependents.get(completed_pipeline_id))
        logging.info("DEPENDENTS Pipeline: %s <- %s", completed_pipeline_id, filter)
    else:
        filter = ('',)

    for spec in pipelines(filter):  # type: PipelineSpec
        all_pipeline_ids.add(spec.pipeline_id)
        ps = status.get(spec.pipeline_id)

        if action == 'init':
            ps.init(spec.pipeline_details,
                    spec.source_details,
                    spec.validation_errors,
                    spec.cache_hash)
            for dep in spec.dependencies:
                dependents.setdefault(dep, set()).add(spec.pipeline_id)
            dependencies[spec.pipeline_id] = spec.dependencies

        elif action == 'update':
            if spec.pipeline_id not in status_all_pipeline_ids:
                ps.init(spec.pipeline_details,
                        spec.source_details,
                        spec.validation_errors,
                        spec.cache_hash)
                for dep in spec.dependencies:
                    dependents.setdefault(dep, set()).add(spec.pipeline_id)
                dependencies[spec.pipeline_id] = spec.dependencies
                logging.info("NEW Pipeline: %s", spec)
            logging.debug('Pipeline: %s (dirty: %s, %s != %s?)',
                          spec.pipeline_id, ps.dirty(), executed_hashes.get(spec.pipeline_id), spec.cache_hash)

        elif action == 'complete':
            if completed_pipeline_id in spec.dependencies:
                ps.init(spec.pipeline_details,
                        spec.source_details,
                        spec.validation_errors,
                        spec.cache_hash)
                logging.info("DEPENDENT Pipeline: %s (%d errors) (from ...%s)",
                             spec.pipeline_id, len(spec.validation_errors), os.path.basename(completed_pipeline_id))

        psle = ps.get_last_execution()
        last_successful = psle.success is True if psle is not None else False
        if ps.runnable() and \
                    (ps.dirty() or
                     (completed_trigger=='scheduled') or
                     (action=='init' and not last_successful)):
            queued = queue_pipeline(spec, 'dirty-task-%s' % action if completed_trigger is None else completed_trigger)
            if queued:
                executed_count += 1
                if executed_count == 4 and action == 'update':
                    # Limit ops on update only
                    break

    if executed_count == 0 and action != 'complete':
        extra_pipelines = status_all_pipeline_ids.difference(all_pipeline_ids)
        for pipeline_id in extra_pipelines:
            logging.info("Removing Pipeline: %s", pipeline_id)
            status.deregister(pipeline_id)


@celery_app.task
def execute_scheduled_pipeline(pipeline_id):
    for spec in pipelines():
        if spec.pipeline_id == pipeline_id:
            queue_pipeline(spec, 'scheduled')


@celery_app.task
def execute_pipeline_task(pipeline_id,
                          pipeline_details,
                          pipeline_cwd,
                          trigger,
                          execution_id):

    spec = PipelineSpec(pipeline_id=pipeline_id,
                        pipeline_details=pipeline_details,
                        path=pipeline_cwd)
    success, _, _ = \
        execute_pipeline(spec,
                         execution_id,
                         trigger,
                         False)

    if success:
        update_pipelines.delay('complete', pipeline_id, trigger)
