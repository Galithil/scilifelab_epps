#!/usr/bin/env python
DESC = """
This file together with manage_demux_stats_thresholds.py performs the "bclconversion" step of LIMS workflow.
In common tongue, it:
 
Fetches info from the sequencing process (RunID, FCID; derives instrument and data type)
Assigns (Q30, Clust per Lane) thresholds to the process (workflow step)
Reformats laneBarcode.html to "demuxstats_FCID.csv" for usage of other applications
Assigns a lot of info from laneBarcode.html to individual samples of the process (e.g. %PF)
Flags samples as QC PASSED/FAILED based on thresholds

Written by Isak Sylvin; isak.sylvin@scilifelab.se"""

#Fetched from SciLife repos
from genologics.lims import Lims
from genologics.config import BASEURI, USERNAME, PASSWORD
from genologics.entities import Process
import flowcell_parser.classes as classes
from manage_demux_stats_thresholds import Thresholds

#Standard packages
from shutil import move
import re
import os 
import csv
import sys
import logging
from argparse import ArgumentParser
logger = logging.getLogger('demux_logger')

def problem_handler(type, message):
    if type == "exit":
        logger.error(message)
        sys.exit(message)
    elif type == "warning":
        logger.warning(message)
        sys.stderr.write(message)
    else:
        logger.info(message)

"""Fetches overarching workflow info"""
def manipulate_workflow(demux_process):
    run_types = {"MiSeq Run (MiSeq) 4.0","Illumina Sequencing (Illumina SBS) 4.0","Illumina Sequencing (HiSeq X) 1.0"}
    try:
        workflow = lims.get_processes(inputartifactlimsid = demux_process.all_inputs()[0].id, type=run_types)[0]
    except Exception as e:
        problem_handler("exit", "Undefined prior workflow step (run type): {}".format(e.message))
    #Copies LIMS sequencing step content
    proc_stats = dict(workflow.udf.items())
    #Instrument is denoted the way it is since it is also used to find
    #the folder of the laneBarcode.html file
    if "MiSeq Run (MiSeq) 4.0" == workflow.type.name:
        proc_stats["Chemistry"] ="MiSeq"
        proc_stats["Instrument"] = "miseq"
    elif "Illumina Sequencing (Illumina SBS) 4.0" == workflow.type.name:
        try:
            proc_stats["Chemistry"] = workflow.udf["Flow Cell Version"]
        except Exception as e:
            problem_handler("exit", "No flowcell version set in sequencing step: {}".format(e.message))
        proc_stats["Instrument"] = "hiseq"
    elif "Illumina Sequencing (HiSeq X) 1.0" == workflow.type.name:
        proc_stats["Chemistry"] ="HiSeqX v2.5"
        proc_stats["Instrument"] = "HiSeq_X"
    else:
        problem_handler("exit", "Unhandled workflow step (run type)")
    logger.info("Run type/chemistry set to {}".format(proc_stats["Chemistry"]))
    logger.info("Instrument set to {}".format(proc_stats["Instrument"]))
    
    try:
        proc_stats["Paired"] = False
    except Exception as e:
        problem_handler("exit", "Unable to fetch workflow information: {}".format(e.message))
    if "Read 2 Cycles" in proc_stats:
        proc_stats["Paired"] = True
    logger.info("Paired libraries: {}".format(proc_stats["Paired"]))  
    #Assignment to make usage more explicit
    try:
        proc_stats["Read Length"] = proc_stats["Read 1 Cycles"]
    except Exception as e:
        problem_handler("exit", "Read 1 Cycles not found. Unable to read Read Length: {}".format(e.message))
    logger.info("Read length set to {}".format(proc_stats["Read Length"]))
    return proc_stats

"""Sets run thresholds"""
def manipulate_process(demux_process, proc_stats):      
    thresholds = Thresholds(proc_stats["Instrument"], proc_stats["Chemistry"], proc_stats["Paired"], proc_stats["Read Length"])
        
    if not "Threshold for % bases >= Q30" in demux_process.udf:
        thresholds.set_Q30()
        try:
            demux_process.udf["Threshold for % bases >= Q30"] = thresholds.Q30
            logger.info("Q30 threshold set to {}".format(demux_process.udf["Threshold for % bases >= Q30"]))
        except Exception as e:
            problem_handler("exit", "Udf improperly formatted. Unable to set Q30 threshold: {}".format(e.message))
    #Would REALLY prefer "Minimum Reads per Lane" over "Threshold for # Reads"
    if not "Minimum Reads per Lane" in demux_process.udf:
        thresholds.set_exp_lane_clust()
        try:
            demux_process.udf["Minimum Reads per Lane"] = thresholds.exp_lane_clust
            logger.info("Minimum clusters per lane set to {}".format(demux_process.udf["Minimum Reads per Lane"]))
        except Exception as e:
            problem_handler("exit", "Udf improperly formatted. Unable to set # Reads threshold: {}".format(e.message))
    
    #Would REALLY prefer "Maximum % Undetermined Reads per Lane" over "Threshold for Undemultiplexed Index Yield"
    if not "Maximum % Undetermined Reads per Lane" in demux_process.udf:
        try:
            demux_process.udf["Maximum % Undetermined Reads per Lane"] = thresholds.undet_indexes_perc
            logger.info("Maximum percentage of undetermined per lane set to {} %".\
                         format(demux_process.udf["Maximum % Undetermined Reads per Lane"]))
        except Exception as e:
            problem_handler("exit", "Udf improperly formatted. Unable to set Undemultiplexed Index Yield threshold: {}".format(e.message))

    #Sets Run ID if not already exists:
    if not "Run ID" in demux_process.udf:
        try:
            demux_process.udf["Run ID"] = proc_stats["Run ID"]
        except Exception as e:
            logger.info("Unable to automatically regenerate Run ID: {}".format(e.message))
    #Checks for document version
    if not "Document Version" in demux_process.udf:
        problem_handler("exit", "No Document Version set. Please set one.")
        
    try:
        demux_process.put()
    except Exception as e:
        problem_handler("exit", "Failed to apply process thresholds to LIMS: {}".format(e.message))
    
"""Sets artifact = samples values """
def set_sample_values(demux_process, parser_struct, proc_stats):
    failed_entries = 0
    noIndex = False
    undet_included = False
    noIndex = False
    undet_lanes = list()
    proj_pattern = re.compile('(P\w+_\d+)')
    #Necessary for noindexruns, should always resolve
    try:
        run_types = {"MiSeq Run (MiSeq) 4.0","Illumina Sequencing (Illumina SBS) 4.0","Illumina Sequencing (HiSeq X) 1.0"}
        seqstep = lims.get_processes(inputartifactlimsid = demux_process.all_inputs()[0].id, type=run_types)[0]
    except Exception as e:
        problem_handler("exit", "Undefined prior workflow step (run type): {}".format(e.message))
    
    if "Lanes to include undetermined" in demux_process.udf:
        try:
            undet_lanes= re.split('[ ,.]', demux_process.udf["Lanes to include undetermined"])
            undet_lanes = [int(i) for i in undet_lanes]
        except:
            problem_handler("exit", "Unable to typecast included undetermined lanes. Possibly non-number in list")
    
    for pool in demux_process.all_inputs():
        undet_reads = 0
        lane_reads = 0
        undet_lane_reads = 0
        
        try:
            outarts_per_lane = demux_process.outputs_per_input(pool.id, ResultFile = True)
        except Exception as e:
            problem_handler("exit", "Unable to fetch artifacts of process: {}".format(e.message))
        if proc_stats["Instrument"] == "miseq":
            lane_no = "1"
        else:
            try:
                lane_no = pool.location[1][0]
            except Exception as e:
                problem_handler("exit", "Unable to determine lane number. Incorrect location variable in process: {}".format(e.message))
        logger.info("Lane number set to {}".format(lane_no))
        exp_smp_per_lne = round(demux_process.udf["Minimum Reads per Lane"]/float(len(outarts_per_lane)), 0)
        logger.info("Expected sample clusters for this lane: {}".format(exp_smp_per_lne))
        
        for target_file in outarts_per_lane:
            try:
                current_name = target_file.samples[0].name
            except Exception as e:
                problem_handler("exit", "Unable to determine sample name. Incorrect sample variable in process: {}".format(e.message))
            for entry in parser_struct:
                if lane_no == entry["Lane"]:
                    
                    sample = entry["Sample"]
                    #Finds name subset "P Anything Underscore Digits"
                    if sample != "Undetermined":
                        sample = proj_pattern.search(sample).group(0)
                        
                    if entry['Barcode sequence'] == "unknown" and sample != "Undetermined":
                        noIndex = True
                        if undet_included:
                            problem_handler("error", "Logical error, undetermined cannot be included for a noIndex lane!")
                                                                                                                   
                    #Bracket for adding undetermined to results   
                    if not sample == 'Undetermined' and int(lane_no) in undet_lanes:
                        undet_included = True
                        #Sanity check for including undetermined
                        #Next entry is undetermined and previous is for a different lane
                        current_index = parser_struct.index(entry)
                        undet = parser_struct[current_index + 1]
                        if undet['Sample'] == 'Undetermined' and parser_struct[current_index - 1]['Lane'] != lane_no:
                            try:
                                clusterType = None
                                if "PF Clusters" in undet:
                                    clusterType = "PF Clusters"
                                else:
                                    clusterType = "Clusters"
                                #Paired runs are divided by two within flowcell parser
                                if proc_stats["Paired"]:
                                    undet_reads = int(undet[clusterType].replace(",",""))*2
                                #Since a single ended run has no pairs, pairs is set to equal reads
                                else:
                                    undet_reads = int(undet[clusterType].replace(",",""))
                                logger.info("Included undetermined for lane number {}".format(lane_no))
                            except Exception as e:
                                problem_handler("exit", "Unable to set values for undetermined #Reads and #Read Pairs: {}".format(e.message))
                        else:
                            problem_handler("exit", "Undetermined for lane {} requested, which has more than one sample".format(lane_no))
                    
                    #Bracket for adding typical sample info        
                    if sample == current_name:
                        logger.info("Added the following set of values to {} of lane {}:".format(sample,lane_no))
                        try:
                            def_atr = {"% of thelane":"% of Raw Clusters Per Lane", "% Perfectbarcode":"% Perfect Index Read", 
                                       "% One mismatchbarcode":"% One Mismatch Reads (Index)", "Yield (Mbases)":"Yield PF (Gb)", 
                                       "% PFClusters":"%PF", "Mean QualityScore":"Ave Q Score", "% >= Q30bases":"% Bases >=Q30"}
                            for old_attr, attr in def_atr.items():
                                #Sets default value for unwritten fields
                                if entry[old_attr] == "" or entry[old_attr] == "NaN":
                                    if old_attr == "% of Raw Clusters Per Lane":
                                        default_value = 100.0
                                    else:
                                        default_value = 0.0
                                    target_file.udf[attr] = default_value
                                    logger.info("{} field not found. Setting default value: {}".format(attr, default_value))
                                else:
                                    #Yields needs division by 1K, is also non-percentage
                                    if old_attr == "Yield (Mbases)":
                                        target_file.udf[attr] = float(entry[old_attr].replace(",",""))/1000
                                        logger.info("{} {}".format(target_file.udf[attr],attr))
                                    else:
                                        target_file.udf[attr] = float(entry[old_attr])
                                        logger.info("{} {}".format(target_file.udf[attr], attr))

                        except Exception as e:
                            problem_handler("exit", "Unable to set artifact values. Check laneBarcode.html for odd values: {}".format(e.message))
                            
                        #Fetches clusters for noIndex runs from sequencing step
                        if noIndex:                         
                            try:
                                for inp in seqstep.all_inputs():
                                    #If reads in seq step, and the lane is equal to the current lane
                                    if inp.location[1][0] == lane_no and "Clusters PF R1" in inp.udf:
                                        if proc_stats["Paired"]:
                                            target_file.udf["# Reads"] = inp.udf["Clusters PF R1"]
                                            target_file.udf["# Read Pairs"] = target_file.udf["# Reads"]/2
                                        else:
                                            target_file.udf["# Reads"] = inp.udf["Clusters PF R1"]*2
                                            target_file.udf["# Read Pairs"] = target_file.udf["# Reads"]/2
                            except Exception as e:
                                problem_handler("exit", "Unable to set values for #Reads and #Read Pairs for perceived noIndex lane: {}".format(e.message))
                                        
                        #Fetches clusters from laneBarcode.html file
                        elif not noIndex:
                            try:
                                clusterType = None
                                if "PF Clusters" in entry:
                                    clusterType = "PF Clusters"
                                else:
                                    clusterType = "Clusters"
                                #Paired runs are divided by two within flowcell parser
                                basenumber = int(entry[clusterType].replace(",",""))
                                if proc_stats["Paired"]:
                                    #Undet always 0 unless manually included
                                    target_file.udf["# Reads"] = basenumber*2 + undet_reads
                                    target_file.udf["# Read Pairs"] = basenumber + undet_reads/2
                                #Since a single ended run has no pairs, pairs is set to equal reads
                                else:
                                    #Undet always 0 unless manually included
                                    target_file.udf["# Reads"] = basenumber + undet_reads
                                    target_file.udf["# Read Pairs"] = target_file.udf["# Reads"]
                            except Exception as e:
                                problem_handler("exit", "Unable to set values for #Reads and #Read Pairs: {}".format(e.message))
                        lane_reads = lane_reads + target_file.udf["# Reads"]
                        logger.info("{}# Reads".format(target_file.udf["# Reads"]))
                        logger.info("{}# Read Pairs".format(target_file.udf["# Read Pairs"]))
                        
                        #Applies thresholds to samples
                        try:
                            if (demux_process.udf["Threshold for % bases >= Q30"] <= float(entry["% >= Q30bases"]) and 
                            int(exp_smp_per_lne) <= target_file.udf["# Reads"] ):
                                target_file.udf["Include reads"] = "YES"
                                target_file.qc_flag = "PASSED"           
                            else:
                                target_file.udf["Include reads"] = "NO"
                                target_file.qc_flag = "FAILED"
                                failed_entries = failed_entries + 1
                            logger.info("Q30 %: {}% found, minimum at {}%".\
                                         format(float(entry["% >= Q30bases"]), demux_process.udf["Threshold for % bases >= Q30"]))
                            logger.info("Expected reads: {} found, minimum at {}".format(target_file.udf["# Reads"], int(exp_smp_per_lne))) 
                            logger.info("Sample QC status set to {}".format(target_file.qc_flag))
                        except Exception as e:
                            problem_handler("exit", "Unable to set QC status for sample: {}".format(e.message))
                    
                    #Counts undetermined
                    elif sample == "Undetermined":
                        clusterType = None
                        if "PF Clusters" in entry:
                            clusterType = "PF Clusters"
                        else:
                            clusterType = "Clusters"
                            
                        if proc_stats["Paired"]:
                            undet_lane_reads = int(entry[clusterType].replace(",",""))*2
                        else:
                            undet_lane_reads = int(entry[clusterType].replace(",",""))
                        
            try: 
                target_file.put()
            except Exception as e:
                problem_handler("exit", "Failed to apply artifact data to LIMS. Possibly due to data in laneBarcode.html; {}".format(e.message))
        
        #Counts undetermined per lane
        if not undet_included:
            try:
                found_undet = round(float(undet_lane_reads)/(lane_reads+undet_lane_reads)*100, 2) 
            #Only plausible error situation. Avoids zero division
            except Exception as e:
                problem_handler("error", "BCLConverter parsing error. No reads detected for lane {}.".format(lane_no))
            #If undetermined reads are greater than threshold*reads_in_lane
            if not noIndex:
                if found_undet > demux_process.udf["Maximum % Undetermined Reads per Lane"]:
                    problem_handler("warning", "Undemultiplexed reads for lane {} was {} ({})% thus exceeding defined limit." \
                                   .format(lane_no, undet_lane_reads, found_undet))
                else:   
                    logger.info("Found {} ({}%) undemultiplexed reads for lane {}.".format(undet_lane_reads, found_undet, lane_no))
    if undet_included:
        problem_handler("warning", "Undetermined reads included in read count!")
    if failed_entries > 0:
        problem_handler("warning", "{} entries failed automatic QC".format(failed_entries))

"""Creates demux_{FCID}.csv and attaches it to process"""
def write_demuxfile(proc_stats, demux_id):
    #Includes windows drive letter support
    datafolder = "{}_data".format(proc_stats["Instrument"])
    lanebc_path = os.path.join(os.sep,"srv","mfs", datafolder,proc_stats["Run ID"],"laneBarcode.html")
    try:
        laneBC = classes.LaneBarcodeParser(lanebc_path)
    except Exception as e:
        problem_handler("exit", "Unable to fetch laneBarcode.html from {}: {}".format(lanebc_path, e.message))
    fname = "{}_demuxstats_{}.csv".format(demux_id, proc_stats["Flow Cell ID"])
    
    #Writes less undetermined info than undemultiplex_index.py. May cause problems downstreams
    with open(fname, "w") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["Project", "Sample ID", "Lane", "# Reads", "Index","Index name", "% of >= Q30 Bases (PF)"])
        for entry in laneBC.sample_data:
            index_name = ""
            if "PF Clusters" in entry:
                reads = entry["PF Clusters"]
            else: 
                reads = entry["Clusters"]
                
            if proc_stats["Paired"]:
                reads = int(reads.replace(",",""))*2
            else:
                reads = int(reads.replace(",","")) 
            
            try:
                writer.writerow([entry["Project"],entry["Sample"],entry["Lane"],reads, \
                                 entry["Barcode sequence"],index_name,entry["% >= Q30bases"]])
            except Exception as e:
                problem_handler("exit", "Flowcell parser is unable to fetch all necessary fields for demux file: {}".format(e.message))
    return laneBC.sample_data

def main(process_lims_id, demux_id, log_id):
    #Sets up logger
    basic_name = "{}_logfile.txt".format(log_id)
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(basic_name)
    fh.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    fh.setFormatter(formatter)
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    logger.addHandler(fh)
    
    logger.info("--process_lims_id {} --demux_id {} --log_id {}".format(process_lims_id, demux_id, log_id))
    
    demux_process = Process(lims,id = process_lims_id)
    #Fetches info on "workflow" level
    proc_stats = manipulate_workflow(demux_process)
    #Sets up the process values
    manipulate_process(demux_process, proc_stats)
    #Create the demux output file
    parser_struct = write_demuxfile(proc_stats, demux_id)
    #Alters artifacts
    set_sample_values(demux_process, parser_struct, proc_stats)
    
    #Changing log file name, can't do this step earlier since proc_stats is made during runtime.
    new_name = "{}_logfile_{}.txt".format(log_id, proc_stats["Flow Cell ID"])
    move(basic_name, new_name)
    
if __name__ =="__main__":
    parser = ArgumentParser(description=DESC)
    parser.add_argument('--process_lims_id',required=True,dest = 'process_lims_id',
                        help="Lims ID of process. Example:24-92373")
    parser.add_argument('--demux_id',required=True,dest = 'demux_id',
                        help=("Id prefix for demux output."))
    parser.add_argument('--log_id',required=True,dest = 'log_id',                 
                        help=("Id prefix for logfile"))
    args = parser.parse_args()
    lims = Lims(BASEURI, USERNAME, PASSWORD)
    lims.check_version()
    main(args.process_lims_id, args.demux_id, args.log_id)

