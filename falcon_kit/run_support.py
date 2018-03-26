from __future__ import absolute_import


from future.utils import viewitems

from . import bash
from .io import NativeIO
from .util.system import (make_fofn_abs, make_dirs, cd)
import json
import logging
import logging.config
import os
import re
import io
import sys
import tempfile
import time
import uuid
import warnings

logger = logging.getLogger(__name__)

from ConfigParser import SafeConfigParser as ConfigParser


def _prepend_env_paths(content, names):
    """
    E.g.
      names = ['PATH', 'PYTYHONPATH']
      content =
        echo hi
      =>
        export PATH=current:path:${PATH}
        export PYTHON=current:path:${PYTHONPATH}
        echo hi
    """
    export_env_vars = ['export %(k)s=%(v)s:${%(k)s}' % dict(
        k=name, v=os.environ.get(name, '')) for name in names]
    return '\n'.join(export_env_vars + [content])


def update_env_in_script(fn, names):
    """Modify fn using on prepend_env_paths().
    """
    with open(fn) as ifs:
        content = ifs.read()
    content = _prepend_env_paths(content, names)
    with open(fn, 'w') as ofs:
        ofs.write(content)


def use_tmpdir_for_files(basenames, src_dir, link_dir):
    """NOT USED. Kept only for reference. This will be done in pypeFLOW.

    Generate script to copy db files to tmpdir (for speed).
    - Choose tmp_dir, based on src_dir name.
    - rsync basenames into tmp_dir  # after 'flock', per file
    - symlink from link_dir into tmp_dir.
    Return list of script lines, sans linefeed.
    """
    script = list()
    unique = os.path.abspath(src_dir).replace('/', '_')
    root = tempfile.gettempdir()
    tmp_dir = os.path.join(root, 'falcon', unique)
    script.append('mkdir -p %s' % tmp_dir)
    for basename in basenames:
        src = os.path.join(src_dir, basename)
        dst = os.path.join(tmp_dir, basename)
        rm_cmd = 'rm -f %s' % basename
        # Wait on lock for up to 10 minutes, in case of very large files.
        rsync_cmd = "flock -w 600 %s.lock -c 'rsync -av %s %s'" % (
            dst, src, dst)
        ln_cmd = 'ln -sf %s %s' % (dst, basename)
        script.extend([rm_cmd, rsync_cmd, ln_cmd])
    return script


def make_job_data(url, script_fn):
    """Choose defaults.
    Run in same directory as script_fn.
    Base job_name on script_fn.
    """
    wd = os.path.dirname(script_fn)
    job_name = '{0}-{1}-{2}'.format(
        os.path.basename(script_fn),
        url.split("/")[-1],
        str(uuid.uuid4())[:8],
    )
    job_data = {"job_name": job_name,
                "cwd": wd,
                "script_fn": script_fn}
    return job_data


def update_HPCdaligner_option(option):
    if '-dal' in option:
        logger.warning(
            'HPC.daligner option "-dal" has changed to "-B". Correcting this for you.')
        option = option.replace('-dal', '-B')
    if '-deg' in option:
        logger.warning(
            'HPC.daligner option "-deg" has changed to "-D". Correcting this for you.')
        option = option.replace('-deg', '-D')
    return option


def validate_config_dict(cd):
    pass


def get_config(config):
    """Temporary version for pbsmrtpipe.
    This will add missing (but curently required) options and use
    get_dict_from_old_falcon_cfg() below.
    The plan is to pass a simpler config from pbsmrtpipe,
    but that will be in a different commit.
    Side-effect: Update 'config'.
    """
    section = 'General'

    def add(name, val):
        if not config.has_option(section, name):
            config.set(section, name, val)
    add('input_fofn', 'NA')
    add('target', 'assembly')
    # add('sge_option', 'NA') # Needed for PBS, but not for everything
    add('sge_option_da', 'NA')
    add('sge_option_la', 'NA')
    add('sge_option_pda', 'NA')
    add('sge_option_pla', 'NA')
    add('sge_option_fc', 'NA')
    add('sge_option_cns', 'NA')
    return get_dict_from_old_falcon_cfg(config)


def dict2config(jdict, section):
    config = ConfigParser()
    if not config.has_section(section):
        config.add_section(section)
    for (k, v) in viewitems(jdict):
        config.set(section, k, str(v))
    return config


def parse_config(config_fn):
    """Return ConfigParser object.
    """
    ext = os.path.splitext(config_fn)[1]
    if ext in ('.json', '.js'):
        jdict = json.loads(open(config_fn).read())
        config = dict2config(jdict, "General")
    else:
        config = ConfigParser() #strict=False?
        config.readfp(open(config_fn))
    return config

def parse_cfg_file(config_fn):
    """Return as dict.
    """
    config = get_dict_from_old_falcon_cfg(
        parse_config(config_fn))
    # New: Parse sections too (and case-insensitively), into sub-dicts.
    with open(config_fn) as stream:
        cfg2 = parse_cfg_with_sections(stream)
        update_config_from_sections(config, cfg2)
    update_job_sections(config)
    return config

def update_job_sections(config):
    """Some crap for backwards compatibility with stuff from 'General' section.
    """
    General = config['General']

    pwatcher_type = General.get('pwatcher_type', config.get('pwatcher_type'))
    job_type = General.get('job_type', '').lower()
    job_queue = General.get('job_queue', '')
    sge_option = General.get('sge_option', '')
    if 'submit' not in config['job.defaults']:
        if 'blocking' == pwatcher_type:
            config['job.defaults']['submit'] = General['job_queue']
        elif 'fs_based' == pwatcher_type or 'network_based' == pwatcher_type:
            if not job_type:
                raise Exception('job.defaults.submit is not set; General.pwatcher_type={}; but General.job_type is not set. Maybe try "job_type=local" first.'.format(pwatcher_type))
            allowed_job_types = ['sge', 'pbs', 'torque', 'slurm', 'lsf', 'local']
            assert job_type in allowed_job_types, 'job_type={} not in {}'.format(
                    job_type, allowed_job_types)
            if job_queue and 'JOB_QUEUE' not in config['job.defaults']:
                config['job.defaults']['JOB_QUEUE'] = job_queue
        else:
            raise Exception('Unknown pwatcher_type={}'.format(pwatcher_type))
    #assert 'submit' in config['job.defaults'], repr(config)
    if sge_option and 'JOB_OPTS' not in config['job.defaults']:
        config['job.defaults']['JOB_OPTS'] = sge_option
    if 'njobs' not in config['job.defaults']:
        config['job.defaults']['njobs'] = int(General.get('default_concurrent_jobs', 8)) # GLOBAL DEFAULT CONCURRENCY
    legacy_names = [
            'pwatcher_type', 'pwatcher_directory',
            'job_type', 'job_queue', 'job_name_style',
            'use_tmpdir',
    ]
    def update_if_missing(name, sub_dict):
        if General.get(name) and name not in sub_dict:
            sub_dict[name] = General[name]
    for name in legacy_names:
        update_if_missing(name, config['job.defaults'])
    # Update a few where the names change and the section is non-default.
    def update_step_job_opts(name):
        if General.get('sge_option_'+name) and 'JOB_OPTS' not in config['job.step.'+name]:
            config['job.step.'+name]['JOB_OPTS'] = General['sge_option_'+name]
    def update_step_njobs(name):
        if General.get(name+'_concurrent_jobs') and 'njobs' not in config['job.step.'+name]:
            config['job.step.'+name]['njobs'] = int(General[name+'_concurrent_jobs'])
    for name in ['da', 'la', 'pda', 'pla', 'cns', 'fc', 'asm']:
        update_step_job_opts(name)
        update_step_njobs(name)
    # Prefer 'asm' to 'fc'.
    asm = dict(config['job.step.asm'])
    config['job.step.asm'] = config['job.step.fc']
    del config['job.step.fc']
    config['job.step.asm'].update(asm)

def parse_cfg_with_sections(stream):
    """Return as dict of dict of ...
    """
    #Experimental:
    """
    ConfigParser sections become sub-sub sections when separated by dots.

        [foo.bar]
        baz = 42

    is equivalent to JSON

        {"foo": {"bar": {"baz": 42}}}
    """
    content = stream.read()
    result = dict()
    try:
        jdict = json.loads(NativeIO(content).read())
        return jdict
    except ValueError:
        pass #logger.exception('Could not parse stream as JSON.')
    try:
        config = ConfigParser() #strict=False?
        config.optionxform = str
        config.readfp(NativeIO(content))
        sections = config.sections()
        for sec in sections:
            result[sec] = dict(config.items(sec))
        return result
    except:
        raise


def update_config_from_sections(config, cfg):
    allowed_sections = set(['General',
            'job.step.da', 'job.step.pda',
            'job.step.la', 'job.step.pla',
            'job.step.cns', 'job.step.fc',
            'job.step.asm',
            'job.defaults',
    ])
    all_sections = set(k for k,v in cfg.items() if isinstance(v, dict))
    unexpected = all_sections - allowed_sections
    if unexpected:
        msg = 'You have {} unexpected cfg sections: {}'.format(
            len(unexpected), unexpected)
        raise Exception(msg)
    config.update(cfg)
    # Guarantee they all exist.
    for sec in allowed_sections:
        if sec not in config:
            config[sec] = dict()


def get_dict_from_old_falcon_cfg(config):
    job_type = "SGE"
    section = 'General'

    input_type = "raw"
    if config.has_option(section, 'input_type'):
        input_type = config.get(section, 'input_type')

    overlap_filtering_setting = """--max_diff 1000 --max_cov 1000 --min_cov 2"""
    if config.has_option(section, 'overlap_filtering_setting'):
        overlap_filtering_setting = config.get(
            section, 'overlap_filtering_setting')

    pa_HPCdaligner_option = """-v -D24 -t16 -e.70 -l1000 -s100"""
    if config.has_option(section, 'pa_HPCdaligner_option'):
        pa_HPCdaligner_option = config.get(section, 'pa_HPCdaligner_option')

    ovlp_HPCdaligner_option = """ -v -D24 -t32 -h60 -e.96 -l500 -s1000"""
    if config.has_option(section, 'ovlp_HPCdaligner_option'):
        ovlp_HPCdaligner_option = config.get(
            section, 'ovlp_HPCdaligner_option')

    pa_HPCdaligner_option = update_HPCdaligner_option(pa_HPCdaligner_option)
    ovlp_HPCdaligner_option = update_HPCdaligner_option(
        ovlp_HPCdaligner_option)

    pa_DBsplit_option = """ -x500 -s200"""
    if config.has_option(section, 'pa_DBsplit_option'):
        pa_DBsplit_option = config.get(section, 'pa_DBsplit_option')

    skip_checks = False
    if config.has_option(section, 'skip_checks'):
        skip_checks = config.getboolean(section, 'skip_checks')

    if config.has_option(section, 'dust'):
        warnings.warn(
            "The 'dust' option is deprecated and ignored. We always run DBdust now. Use pa_DBdust_option to override its default arguments.")

    #pa_DBdust_option = "-w128 -t2.5 -m20"
    pa_DBdust_option = ""  # Gene recommends the defaults.
    if config.has_option(section, 'pa_DBdust_option'):
        pa_DBdust_option = config.get(section, 'pa_DBdust_option')

    dazcon = False
    if config.has_option(section, 'dazcon'):
        dazcon = config.getboolean(section, 'dazcon')

    pa_dazcon_option = "-j 4 -x -l 500"
    if config.has_option(section, 'pa_dazcon_option'):
        pa_dazcon_option = config.get(section, 'pa_dazcon_option')

    # DAMASKER options
    """
    Example config usage:
    pa_use_tanmask = true
    pa_use_repmask = true
    pa_HPCtanmask_option =
    pa_repmask_levels = 2
    pa_HPCrepmask_1_option = -g1 -c20 -mtan
    pa_HPCrepmask_2_option = -g10 -c15 -mtan -mrep1
    pa_damasker_HPCdaligner_option = -mtan -mrep1 -mrep10
    """
    pa_use_tanmask = False
    if config.has_option(section, 'pa_use_tanmask'):
        pa_use_tanmask = config.getboolean(section, 'pa_use_tanmask')

    pa_HPCtanmask_option = ""
    if config.has_option(section, 'pa_HPCtanmask_option'):
        pa_HPCtanmask_option = config.get(section, 'pa_HPCtanmask_option')

    pa_use_repmask = False
    if config.has_option(section, 'pa_use_repmask'):
        pa_use_repmask = config.getboolean(section, 'pa_use_repmask')

    pa_repmask_levels = 0   # REPmask tool can be used multiple times.
    if config.has_option(section, 'pa_repmask_levels'):
        pa_repmask_levels = config.getint(section, 'pa_repmask_levels')

    pa_HPCrepmask_1_option = """ -g1 -c20 -mtan"""
    if config.has_option(section, 'pa_HPCrepmask_1_option'):
        pa_HPCrepmask_1_option = config.get(section, 'pa_HPCrepmask_1_option')

    pa_HPCrepmask_2_option = """ -g10 -c15 -mtan -mrep1"""
    if config.has_option(section, 'pa_HPCrepmask_2_option'):
        pa_HPCrepmask_2_option = config.get(section, 'pa_HPCrepmask_2_option')

    pa_damasker_HPCdaligner_option = """ -mtan -mrep1 -mrep10"""    # Repeat masks need to be passed to Daligner.
    if config.has_option(section, 'pa_damasker_HPCdaligner_option'):
        pa_damasker_HPCdaligner_option = config.get(
            section, 'pa_damasker_HPCdaligner_option')
    # End of DAMASKER options.

    ovlp_DBsplit_option = """ -x500 -s200"""
    if config.has_option(section, 'ovlp_DBsplit_option'):
        ovlp_DBsplit_option = config.get(section, 'ovlp_DBsplit_option')

    falcon_sense_option = """ --output_multi --min_idt 0.70 --min_cov 2 --max_n_read 1800 --n_core 6"""
    if config.has_option(section, 'falcon_sense_option'):
        falcon_sense_option = config.get(section, 'falcon_sense_option')
    if 'local_match_count' in falcon_sense_option or 'output_dformat' in falcon_sense_option:
        raise Exception('Please remove obsolete "--local_match_count_*" or "--output_dformat"' +
                        ' from "falcon_sense_option" in your cfg: %s' % repr(falcon_sense_option))

    falcon_sense_skip_contained = False
    if config.has_option(section, 'falcon_sense_skip_contained'):
        falcon_sense_skip_contained = config.getboolean(
            section, 'falcon_sense_skip_contained')

    falcon_sense_greedy = False
    if config.has_option(section, 'falcon_sense_greedy'):
        falcon_sense_greedy = config.getboolean(section, 'falcon_sense_greedy')

    LA4Falcon_preload = ""
    if config.has_option(section, 'la4falcon_preload'):
        LA4Falcon_preload = config.getboolean(section, 'la4falcon_preload')

    genome_size = 0
    if config.has_option(section, 'genome_size'):
        genome_size = config.getint(section, 'genome_size')

    seed_coverage = 20
    if config.has_option(section, 'seed_coverage'):
        seed_coverage = config.getfloat(section, 'seed_coverage')

    length_cutoff = -1
    if config.has_option(section, 'length_cutoff'):
        length_cutoff = config.getint(section, 'length_cutoff')
    if length_cutoff < 0:
        if genome_size < 1:
            raise Exception(
                'Must specify either length_cutoff>0 or genome_size>0')

    length_cutoff_pr = config.getint(section, 'length_cutoff_pr')
    input_fofn_fn = config.get(section, 'input_fofn')

    # This one depends on length_cutoff_pr for its default.
    fc_ovlp_to_graph_option = ''
    if config.has_option(section, 'fc_ovlp_to_graph_option'):
        fc_ovlp_to_graph_option = config.get(
            section, 'fc_ovlp_to_graph_option')
    if '--min_len' not in fc_ovlp_to_graph_option:
        fc_ovlp_to_graph_option += ' --min_len %d' % length_cutoff_pr

    bestn = 12
    if config.has_option(section, 'bestn'):
        bestn = config.getint(section, 'bestn')

    if config.has_option(section, 'target'):
        target = config.get(section, 'target')
        if target not in ["overlapping", "pre-assembly", "assembly"]:
            msg = """ Target has to be "overlapping", "pre-assembly" or "assembly" in this verison. You have an unknown target %s in the configuration file.  """ % target
            raise Exception(msg)
    else:
        logger.info(""" No target specified, assuming "assembly" as target """)
        target = "assembly"

    if config.has_option(section, 'stop_all_jobs_on_failure'):
        stop_all_jobs_on_failure = config.getboolean(
            section, 'stop_all_jobs_on_failure')
    else:
        # Good default. Rarely needed, since we already stop early if *all* tasks fail
        # in a given refresh.
        stop_all_jobs_on_failure = False
    if config.has_option(section, 'use_tmpdir'):
        tmpdir = config.get(section, 'use_tmpdir')
        if '/' in tmpdir:
            tempfile.tempdir = tmpdir
            use_tmpdir = tmpdir
        else:
            use_tmpdir = config.getboolean(section, 'use_tmpdir')
    else:
        use_tmpdir = False

    TEXT_FILE_BUSY = 'avoid_text_file_busy'
    if config.has_option(section, TEXT_FILE_BUSY):
        bash.BUG_avoid_Text_file_busy = config.getboolean(
            section, TEXT_FILE_BUSY)

    hgap_config = {  # "input_fofn_fn" : input_fofn_fn, # deprecated
        "input_fofn": input_fofn_fn,
        "target": target,
        "input_type": input_type,
        "overlap_filtering_setting": overlap_filtering_setting,
        "genome_size": genome_size,
        "seed_coverage": seed_coverage,
        "length_cutoff": length_cutoff,
        "length_cutoff_pr": length_cutoff_pr,
        "pa_HPCdaligner_option": pa_HPCdaligner_option,
        "pa_use_tanmask": pa_use_tanmask,
        "pa_HPCtanmask_option": pa_HPCtanmask_option,
        "pa_use_repmask": pa_use_repmask,
        "pa_repmask_levels": pa_repmask_levels,
        "pa_HPCrepmask_1_option": pa_HPCrepmask_1_option,
        "pa_HPCrepmask_2_option": pa_HPCrepmask_2_option,
        "pa_damasker_HPCdaligner_option": pa_damasker_HPCdaligner_option,
        "ovlp_HPCdaligner_option": ovlp_HPCdaligner_option,
        "pa_DBsplit_option": pa_DBsplit_option,
        "skip_checks": skip_checks,
        "pa_DBdust_option": pa_DBdust_option,
        "dazcon": dazcon,
        "pa_dazcon_option": pa_dazcon_option,
        "ovlp_DBsplit_option": ovlp_DBsplit_option,
        "fc_ovlp_to_graph_option": fc_ovlp_to_graph_option,
        "falcon_sense_option": falcon_sense_option,
        "falcon_sense_skip_contained": falcon_sense_skip_contained,
        "falcon_sense_greedy": falcon_sense_greedy,
        "LA4Falcon_preload": LA4Falcon_preload,
        "stop_all_jobs_on_failure": stop_all_jobs_on_failure,
        "use_tmpdir": use_tmpdir,
        TEXT_FILE_BUSY: bash.BUG_avoid_Text_file_busy,
    }
    possible_extra_keys = [
            'sge_option', 'default_concurrent_jobs',
            'pwatcher_type', 'pwatcher_directory',
            'job_type', 'job_queue', 'job_name_style',
            'use_tmpdir',
    ]
    for step in ['da', 'la', 'pda', 'pla', 'fc', 'cns', 'asm']:
        sge_option_key = 'sge_option_' + step
        possible_extra_keys.append(sge_option_key)
        concurrent_jobs_key = step + '_concurrent_jobs'
        possible_extra_keys.append(concurrent_jobs_key)
    added = list()
    for key in possible_extra_keys:
        if config.has_option(section, key):
            added.append(key)
            hgap_config[key] = config.get(section, key)
    if added:
        added.sort()
        msg = 'You have several old-style options. These should be provided in the `[job.defaults]` or `[job.step.*]` sections, and possibly renamed. See https://github.com/PacificBiosciences/FALCON/wiki/Configuration\n {}'.format(added)
        warnings.warn(msg)

    # Warn on unused variables.
    provided = dict(config.items(section))
    unused = set(provided) - set(k.lower() for k in hgap_config)
    if unused:
        warnings.warn("Unexpected keys in input config: %s" % repr(unused))

    hgap_config["install_prefix"] = sys.prefix

    return hgap_config


default_logging_config = """
[loggers]
keys=root

[handlers]
keys=stream,file_all

[formatters]
keys=form01,form02

[logger_root]
level=NOTSET
handlers=stream,file_all

[handler_stream]
class=StreamHandler
level=INFO
formatter=form02
args=(sys.stderr,)

[handler_file_all]
class=FileHandler
level=DEBUG
formatter=form01
args=('all.log', 'w')

[formatter_form01]
format=%(asctime)s - %(name)s - %(levelname)s - %(message)s

[formatter_form02]
format=[%(levelname)s]%(message)s
"""


def _setup_logging(logging_config_fn):
    """See https://docs.python.org/2/library/logging.config.html
    """
    logging.Formatter.converter = time.gmtime  # cannot be done in .ini

    if logging_config_fn:
        if logging_config_fn.endswith('.json'):
            logging.config.dictConfig(
                json.loads(open(logging_config_fn).read()))
            # print repr(logging.Logger.manager.loggerDict) # to debug
            return
        logger_fileobj = open(logging_config_fn)
    else:
        logger_fileobj = NativeIO(default_logging_config)
    defaults = {
    }
    logging.config.fileConfig(
        logger_fileobj, defaults=defaults, disable_existing_loggers=False)


def setup_logger(logging_config_fn):
    global logger
    try:
        _setup_logging(logging_config_fn)
        logger = logging.getLogger("fc_run")
        logger.info('Setup logging from file "{}".'.format(logging_config_fn))
    except Exception:
        logging.basicConfig()
        logger = logging.getLogger()
        logger.exception(
            'Failed to setup logging from file "{}". Using basicConfig().'.format(logging_config_fn))
    try:
        import logging_tree
        logger.info(logging_tree.format.build_description())
    except ImportError:
        pass

    return logger


def get_nblock(db_file):
    """Return #blocks in dazzler-db.
    """
    nblock = 1
    new_db = True
    if os.path.exists(db_file):
        with open(db_file) as f:
            for l in f:
                l = l.strip().split()
                if l[0] == "blocks" and l[1] == "=":
                    nblock = int(l[2])
                    new_db = False
                    break
    # Ignore new_db for now.
    return nblock


def daligner_gather_las(job_rundirs):
    """Return list of (block, las_fn).
    """
    # Could be L1.* or preads.*
    re_las = re.compile(r'\.(\d*)(\.\d*)?\.las$')
    for job_rundir in job_rundirs:
        # 'out' sub-dir by convention. See run_daligner() above. (Improve that someday.)
        for las_fn in os.listdir(job_rundir):
            mo = re_las.search(las_fn)
            if not mo:
                continue
            # We will merge in the m_* dir of the left block.
            block = int(mo.group(1))
            yield block, os.path.join(job_rundir, las_fn)


def get_length_cutoff(length_cutoff, fn):
    if length_cutoff < 0:
        length_cutoff = int(open(fn).read().strip())
        logger.info('length_cutoff=%d from %r' % (length_cutoff, fn))
    return length_cutoff  # possibly updated


def build_rdb(input_fofn_fn, config, job_done, script_fn, run_jobs_fn):
    run_jobs_fn = os.path.basename(run_jobs_fn)
    script = bash.script_build_rdb(config, input_fofn_fn, run_jobs_fn)
    bash.write_script(script, script_fn, job_done)


def build_pdb(input_fofn_fn, config, job_done, script_fn, run_jobs_fn):
    run_jobs_fn = os.path.basename(run_jobs_fn)
    script = bash.script_build_pdb(config, input_fofn_fn, run_jobs_fn)
    bash.write_script(script, script_fn, job_done)


def run_db2falcon(config, preads4falcon_fn, preads_db, job_done, script_fn):
    script = bash.script_run_DB2Falcon(config, preads4falcon_fn, preads_db)
    bash.write_script(script, script_fn, job_done)


def run_falcon_asm(config, las_fofn_fn, preads4falcon_fasta_fn, db_file_fn, job_done, script_fn):
    script = bash.script_run_falcon_asm(
        config, las_fofn_fn, preads4falcon_fasta_fn, db_file_fn)
    bash.write_script(script, script_fn, job_done)


def run_report_pre_assembly(i_raw_reads_db_fn, i_preads_fofn_fn, genome_length, length_cutoff, o_json_fn, job_done, script_fn):
    script = bash.script_run_report_pre_assembly(
        i_raw_reads_db_fn, i_preads_fofn_fn, genome_length, length_cutoff, o_json_fn)
    bash.write_script(script, script_fn, job_done)


def run_daligner(daligner_script, db_prefix, config, job_done, script_fn):
    bash.write_script(daligner_script, script_fn, job_done)


def run_las_merge(script, job_done, config, script_fn):
    bash.write_script(script, script_fn, job_done)


def run_consensus(db_fn, las_fn, out_file_fn, config, job_done, script_fn):
    script = bash.script_run_consensus(
        config, db_fn, las_fn, os.path.basename(out_file_fn))
    bash.write_script(script, script_fn, job_done)
