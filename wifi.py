#!/usr/bin/env python
""" A widefield imaging pipeline for LOFAR HBA.
"""
import configparser
import glob
import logging
import os
import subprocess
import sys
import traceback

from astropy import units as u
from astropy.io import ascii
from astropy.io import fits
from regions import DS9Parser, write_ds9

import bdsf
import casacore.tables as ct
import numpy as np
from make_extended_mask import make_extended_mask, merge_mask


def die(reason=''):
    ''' Stop the pipeline, reporting an error message if given.

    Args:
        reason (str): the reason for stopping the pipeline.
    Returns:
        None
    '''
    if reason:
        LOGGER.error(reason)
    else:
        LOGGER.error('Something went wrong for an unknown reason!')
    sys.exit(-1)


def get_beam(image):
    ''' Extracts the restoring beam from a FITS image and returns a list containing
    the major and minor axis, and the position angle.

    Args:
        image (str): image from which to extract the restoring beam.
    Returns:
        beam (list): a list with major axis, minor axis and position angle, all in degrees.
    '''
    f = fits.open(image)
    head = f[0].header
    bmaj = head['BMAJ']
    bmin = head['BMIN']
    bpa = head['BPA']
    temp = [bmaj, bmin, bpa]
    beam = list(np.round(temp, 10))

    return beam


def get_mslist():
    ''' Put all measurement sets from the mslist text file into a list.
    '''
    mses = []
    with open(CONFIG['data']['mslist']) as f:
        for l in f.readlines():
            mses.append(l.strip())
    return mses


def is_tapered():
    ''' Checks if the data has already been tapered.

    Returns:
        tapered (bool): `True` if tapering has been done, `False` otherwise.
    '''
    tapered_images = glob.glob('wsclean_taper*.fits')
    tapered = len(tapered_images) > 0
    return tapered

def make_dde_directions(sourcecat, Speak_min = 0.025, parset='')
    skymodel_csv = ascii.read(sourcecat, format='csv', header_start=4, data_start=5)

    Speak = skymodel_csv['Peak_flux']
    Speak_min = 0.0250
    filter_100mJy_peak = Speak > Speak_min

    sub_tab = skymodel_csv['Source_id', 'RA', 'DEC', 'Peak_flux'][filter_100mJy_peak]

    # In case of multiple components of a single source being found, calculate the mean position.
    positions = Table(names=['Source_id', 'RA', 'DEC'])

    from scipy.spatial.distance import pdist, squareform
    from scipy.cluster.hierarchy import linkage, fcluster, dendrogram
    # Make an (N,2) array of directions and compute the distances between points.
    pos = np.stack((list(sub_tab['RA']), list(sub_tab['DEC'])), axis=1)
    distances = pdist(pos, 'euclidean')

    # Cluster components based on the distance between them.
    # Everything within 1 arcmin gets clustered into a direction.
    Z = linkage(pos, method='complete', metric='euclidean')
    clusters = fcluster(Z, 60. / 3600., criterion='distance')
    dendrogram(Z)

    # Loop over the clusters and merge them into single directions.
    for c in np.unique(clusters):
        idx = np.where(clusters == c)
        i = idx[0][0]
        comps = sub_tab[idx]
        if len(comps) == 1:
            # Nothing needs to merge with this direction.
            positions.add_row((sub_tab['Source_id'][i], sub_tab['RA'][i], sub_tab['DEC'][i]))
            continue
        else:
            ra_mean = np.mean(sub_tab['RA'][idx])
            dec_mean = np.mean(sub_tab['DEC'][idx])
            if (ra_mean not in positions['RA']) and (dec_mean not in positions['DEC']):
                positions.add_row((sub_tab['Source_id'][i], ra_mean, dec_mean))
            else:
                print('Direction {:d} has been merged already.\n'.format(sub_tab['Source_id'][i]))
    positions_25mJy = positions
    print('{:d} sources with peak flux >{:f} Jy'.format(len(positions), Speak_min))

    # Write parsets with 10 directions per parset.
    print('DPPP friendly format:')
    msnamelist = list(map(lambda s: 'P{:d}.ms'.format(int(s)), positions_25mJy['Source_id']))
    print('msout.name=[' + ','.join(msnamelist) + ']')
    msposlist = list(map(lambda x: '[{:f}deg,{:f}deg]'.format(x[0], x[1]), positions_25mJy['RA', 'DEC']))
    print('phasecenter=[' + ','.join(msposlist) + ']')
    region_strs = map(lambda pos: 'fk5\ncircle({:f},{:f},{:f}) # color=red width=2 text="{:s}"'.format(*pos['RA','DEC'], 30. / 3600, str(pos['Source_id'])), positions_25mJy)
    for i in range(len(msnamelist)//10):
        with open('split_25mJy_{:02d}.parset'.format(i), 'w') as f:
            output = PARSET + '\nmsout.name=[' + ','.join(msnamelist[10*i:10*(i+1)]) + ']\n' +\
                     'shift.phasecenter=[' + ','.join(msposlist[10*i:10*(i+1)]) + ']\n'
            f.write(output)

    from regions import DS9Parser, write_ds9
    parser = DS9Parser('\n'.join(list(region_strs)))
    regions = parser.shapes.to_regions()

    write_ds9(regions, 'pointings_25mJy.reg')

def make_tiles(ra, dec, tile_spacing=0.625, tile_facet_size=0.7):
    ''' Create the tiling for a 0.3'' mosaic of the central 2.5 degree.
    
    Args:
        ra (float): right ascension of the pointing center.
        dec (float): declination of the pointing center.
    Returns:
        facets (ndarray): a 4 x 4 x 2 array with the central RA and DEC of the tiles.
    '''
    spacing = tile_spacing * u.degree
    # Have some overlap
    facet_size = tile_facet_size * u.degree
    facets = np.zeros((4,4,2)) * u.degree#, dtype=(float, 2)) * u.degree
    facetlist = []
    k = 1
    for i in range(4):
        for j in range(4):
            RA = phasecenter.ra + (spacing * (j-1.5) / np.cos((phasecenter.dec + spacing*(i-1.5)).rad))
            DEC = phasecenter.dec + (spacing * (i-1.5))
            facets[i, j, 0] = RA
            facets[i, j, 1] = DEC
            facetlist.append((RA.deg,DEC.deg))
            PARSET = 'msout.storagemanager=dysco\nmsout.storagemanager.databitrate=4\nmsout.storagemanager.weightbitrate=8\nsteps=[shift,avg]\nshift.type = phaseshift\nshift.phasecenter = [{:f}deg, {:f}deg]\navg.type = average\navg.timeresolution = 4\navg.freqresolution = 48.82kHz'.format(RA.deg, DEC.deg)
            with open('shift_to_facet_{:d}.parset'.format(k), 'w') as f:
                f.write(PARSET)
            k += 1
    region_strs = map(lambda pos: 'fk5\nbox({:f},{:f},{:f},{:f},0) # color=green width=4 text=""'.format(*pos, facet_size.value, facet_size.value), facetlist)
    parser = DS9Parser('\n'.join(list(region_strs)))
    regions = parser.shapes.to_regions()
    write_ds9(regions, 'facets.reg')
    return facets

def run_pybdsf(fitsname, detectimage, outcat='skymodel_1asec_lbregion_pybdsf'):
    ''' Run PyBDSF on an image, using standard SKSP settings.

    Two catalogues are written when finished:
        - A CSV catalogue with all columns present.
        - A BBS formatted catalogue, suitable for e.g. DPPP.

    Args:
        fitsname (str): path to the image from which to extract the fluxes.
        detectimage (str): path to the image on which to run source detection.
    Returns:
        None
    '''
    # Pull the reference frequency from the header.
    f = fits.open(fitsname)
    restfrq = f[0].header['CRVAL3']
    # Run PyBDSF with standard SKSP settings.
    res = bdsf.process_image(fitsname, detection_image=detectimage, thresh_isl=4.0, thresh_pix=5.0, rms_box=(150, 15), rms_map=True, mean_map='zero', ini_method='intensity', adaptive_rms_box=True, adaptive_thresh=150, rms_box_bright=(60, 15), group_by_isl=False, group_tol=10.0, output_opts=True, output_all=True, atrous_do=True, atrous_jmax=4, flagging_opts=True, flag_maxsize_fwhm=0.5, advanced_opts=True, blank_limit=None, frequency=restfrq)
    # Write out a catalog.
    res.write_catalog(outfile=outcat + '.csv', bbs_patches='source', catalog_type='gaul', format='csv')
    res.write_catalog(outfile=outcat + '.bbs', bbs_patches='source', catalog_type='gaul', format='bbs')


# One should have run `genericpipeline.py -d -c pipeline.cfg LB-Split-Calibrators.parset` before running this.
# Two datasets must be present:
# - blocks of 10SB for the target field
# - a full bandwidth dataset for the infield calibrator
try:
    # See if there is a running directory defined.
    os.chdir(os.path.expandvars("$RUNDIR"))
except OSError:
    # If not, that's fine, run in the current directory.
    pass
CWD = os.getcwd()

# Set up logging stuff.
logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger()
LOGGER.name = sys.argv[0]

logging.addLevelName(logging.INFO, "\033[1;32m%s\033[1;0m" % logging.getLevelName(logging.INFO))
logging.addLevelName(logging.WARNING, "\033[1;31m%s\033[1;0m" % logging.getLevelName(logging.WARNING))
logging.addLevelName(logging.ERROR, "\033[1;41m%s\033[1;0m" % logging.getLevelName(logging.ERROR))

# Read in the configuration file.
CONFIG = configparser.ConfigParser()
CONFIG.read(sys.argv[1])

LOGGER.info('Checking inputs.')
if not os.path.isdir(CONFIG['data']['highres_data']):
    # Used for msoaicing at 0.2-0.3''.
    # die('High resolution data not found!')
    pass
if not os.path.isfile(CONFIG['solutions']['infield_sols_p']):
    die('Infield calibrator phase solutions not found!')
if not os.path.isfile(CONFIG['solutions']['infield_sols_ap']):
    die('Infield calibrator amp+phase solutions not found!')

MSES = get_mslist()

if CONFIG['data'].getboolean('do_apply_kms'):
    # This will apply the DIS2 solutions from the ddf-pipeline to arrive at DATA_DI_CORRECTED.
    if not os.path.exists(CONFIG['solutions']['kms_solsdir']):
        die('killMS solution directory not found!')
    LOGGER.info('Converting kMS solutions to H5Parm.')
    for ms in MSES:
        sols_npz = CONFIG['solutions']['kms_solsdir'] + '/' + ms + '/killMS.DIS2_full.sols.npz'
        sols_h5 = os.getcwd() + '/' + ms + '_DIS2_full.sols.h5'
        try:
            CMD = 'killMS2H5parm.py {h5:s} {npz:s} --nofulljones'.format(h5=sols_h5, npz=sols_npz)
            LOGGER.info(CMD)
            subprocess.call(CMD, shell=True)
            CMD2 = 'addIS_to_h5.py {h5:s} {ms:s} --solset_in sol000 --solset_out sol001 --do_int_stations'.format(h5=sols_h5, ms=ms)
            LOGGER.info(CMD2)
            subprocess.call(CMD2, shell=True)
        except Exception:
            traceback.print_exc()
            die()

    LOGGER.info('Applying kMS solutions to MS.')
    for ms in MSES:
        with open('apply_kms.parset', 'w') as f:
            sols = os.getcwd() + '/' + ms + '_DIS2_full.sols.h5'
            f.write('msin={ms:s} msin.datacolumn=DATA msout=. msout.datacolumn=DATA_DI_CORRECTED msout.storagemanager=dysco steps=[applykms] applykms.type=applycal applykms.steps=[p,a] applykms.parmdb={h5:s} applykms.solset={ss:s} applykms.p.correction=phase000 applykms.a.correction=amplitude000'.format(dc=CONFIG['data']['data_column'], ms=ms, h5=sols, ss='sol001').replace(' ', '\n'))
        CMD = 'DPPP apply_kms.parset'
        LOGGER.info(CMD)
        subprocess.call(CMD, shell=True)

if CONFIG['data'].getboolean('do_subtract'):
    # Subtract sources outside a given region using the DDS3 solutions from the ddf-pipeline.
    # This is especially important with bright sources outside the FoV of the international stations,
    # but inside that of the Dutch stations.
    # Load the required settings.
    dc = CONFIG['subtract']['subtract_from']
    box = CONFIG['subtract']['boxfile']
    # Copy over the required files.
    path = CONFIG['subtract']['lotss_directory']
    LOGGER.info('Copying over necessary LoTSS products.')
    import shutil
    reqs = ['image_dirin_SSD_m.npy.ClusterCat.npy', 'DDS3_full_5038110493.005561_smoothed.npz', 'DDS3_full_slow_5038110493.005561_merged.npz', 'image_full_ampphase_di_m.NS.DicoModel', 'image_full_ampphase_di_m.NS.mask01.fits', 'SOLSDIR']
    for r in reqs:
        if os.path.isfile(path + '/' + r):
            shutil.copy2(path + '/' + r, os.getcwd() + '/')
        elif os.path.isdir(path + '/' + r):
            shutil.copytree(path + '/' + r, os.getcwd() + '/' + r)
    LOGGER.info('Flagging international stations in all MS.')
    for ms in MSES:
        CMD1 = 'backup_flagtable.py {:s}'.format(ms)
        CMD2 = 'DPPP flag_IS.parset msin={:s}'.format(ms)
        LOGGER.info(CMD1)
        subprocess.call(CMD1, shell=True)
        LOGGER.info(CMD2)
        subprocess.call(CMD2, shell=True)

    CMD = 'sub-sources-outside-region.py -b {:s} -m {:s} -c {:s} -f 1 -t 1 -p keepcenter'.format(box, CONFIG['data']['mslist'], dc)
    LOGGER.info(CMD)
    subprocess.call(CMD, shell=True)

    LOGGER.info('Restoring flags.')
    for ms in MSES:
        CMD3 = 'restore_flagtable.py {:s}'.format(ms)
        LOGGER.info(CMD3)
        subprocess.call(CMD3, shell=True)

if CONFIG['control']['exitafter'] == 'subtract':
    LOGGER.info('Pipeline finished successfully.')
    sys.exit(0)

if CONFIG['data'].getboolean('do_apply_infield'):
    if os.path.isfile(os.getcwd() + '/' + 'image_full_ampphase_di_m.NS_SUB.log'):
        LOGGER.info('Applying infield calibrator solutions: DATA_SUB -> CORRECTED_DATA')
        dc = 'DATA_SUB'
    else:
        LOGGER.info('Applying infield calibrator solutions: {:s} -> CORRECTED_DATA'.format(CONFIG['data']['data_column']))
        dc = CONFIG['data']['data_column']
    for ms in MSES:
        sols_p = CONFIG['solutions']['infield_sols_p']
        if not CONFIG['solutions'].getboolean(['infield_phase_only']):
            sols_ap = CONFIG['solutions']['infield_sols_ap']
            try:
                with open('apply_infield_solutions.parset', 'w') as f:
                    f.write('msin={ms:s} msin.datacolumn={dc:s} msout=. msout.datacolumn=CORRECTED_DATA msout.storagemanager=dysco steps=[applyif1,applyif2] applyif1.type=applycal applyif1.parmdb={h51:s} applyif1.solset={ss1:s} applyif1.correction=phase000 applyif2.type=applycal applyif2.steps=[p,a] applyif2.parmdb={h52:s} applyif2.solset={ss2:s} applyif2.p.correction=phase000 applyif2.a.correction=amplitude000'.format(dc=dc, ms=ms, h51=sols_p, ss1=CONFIG['solutions']['infield_solset'], h52=sols_ap, ss2=CONFIG['solutions']['infield_solset']).replace(' ', '\n'))
                CMD = 'DPPP apply_infield_solutions.parset'
                LOGGER.info(CMD)
                subprocess.call(CMD, shell=True)
            except Exception:
                traceback.print_exc()
                die()
        else:
            try:
                with open('apply_infield_solutions.parset', 'w') as f:
                    f.write('msin={ms:s} msin.datacolumn={dc:s} msout=. msout.datacolumn=CORRECTED_DATA msout.storagemanager=dysco steps=[applyif1] applyif1.type=applycal applyif1.parmdb={h51:s} applyif1.solset={ss1:s} applyif1.correction=phase000'.format(dc=dc, ms=ms, h51=sols_p, ss1=CONFIG['solutions']['infield_solset']).replace(' ', '\n'))
                os.system('DPPP apply_infield_solutions.parset')
            except Exception:
                traceback.print_exc()
                die()
else:
    LOGGER.info('Infield solutions have been applied, skipping applycal step.')

if is_tapered():
    LOGGER.info('Taper has already been created, skipping WSClean step.')
else:
    LOGGER.info('Tapering data to target resolution of {:s}.'.format(CONFIG['image']['taper_full']))
    chan_out = (len(MSES) // 4) + 1
    # Having a small pixel scale is important here, such that all baselines are considered during
    # weighting. WSClean automatically ignores all baselines that provide resolutions higher than
    # the given pixel size.
    CMD = 'wsclean -j {:d} -mem {:d} -data-column {:s} -niter 0 -channels-out {:d} -weight briggs {:s} -size 1024 1024 -scale 0.05asec -minuvw-m 5000 -make-psf -fit-beam -no-reorder -no-update-model-required -store-imaging-weights -taper-gaussian {:s}asec -name wsclean_taper *.{:s}'.format(int(CONFIG['image']['wsclean_ncpu']), int(CONFIG['image']['wsclean_mem']), CONFIG['image']['data_column'], chan_out, CONFIG['image']['robust_full'], CONFIG['image']['taper_full'], MSES[0].split('.')[-1])
    LOGGER.info(CMD)
    subprocess.call(CMD, shell=True)
    for i, ms in enumerate(MSES):
        CMD = 'transfer_imaging_weight.py {:s}'.format(ms)
        LOGGER.info(CMD)
        subprocess.call(CMD, shell=True)
    #BEAM = get_beam('wsclean_taper-MFS-psf.fits')
    #DDF_RESTORING_BEAM = '[{maj:f},{min:f},{pa:f}'.format(BEAM[0], BEAM[1], BEAM[2])
    DDF_RESTORING_BEAM = '1.0'

if os.path.exists(os.getcwd() + '/image_dirin_SSD_init_natural.int.restored.fits'):
    LOGGER.info('Initial widefield image already exists, not recreating.')
else:
    LOGGER.info('Creating {:s}" widefield image.'.format(CONFIG['image']['taper_full']))
    CMD = 'DDF.py --Output-Name=image_dirin_SSD_init_natural --Data-MS={:s} --Deconv-PeakFactor 0.050000 --Data-ColName {ic:s} --Data-ChunkHours 4 --Parallel-NCPU=32 --Beam-CenterNorm=1 --Deconv-CycleFactor=0 --Deconv-MaxMinorIter=10000 --Deconv-MaxMajorIter=1 --Deconv-Mode SSD --Beam-Model=LOFAR --Beam-LOFARBeamMode=A --Weight-Mode Natural  --Image-NPix=25000 --CF-wmax 50000 --CF-Nw 100 --Output-Also onNeds --Image-Cell {cell:f} --Facets-NFacets=7 --SSDClean-NEnlargeData 0 --Freq-NDegridBand 1 --Beam-NBand 1 --Facets-DiamMax 1.5 --Facets-DiamMin 0.1 --Deconv-RMSFactor=3.000000 --SSDClean-ConvFFTSwitch 10000 --Data-Sort 1 --Cache-Dir=. --Log-Memory 1 --GAClean-RMSFactorInitHMP 1.000000 --GAClean-MaxMinorIterInitHMP 10000.000000 --DDESolutions-SolsDir=SOLSDIR --Cache-Weight=reset --Output-Mode=Clean --Output-RestoringBeam {beam:s} --Weight-ColName="IMAGING_WEIGHT" --Freq-NBand=2 --RIME-DecorrMode=FT --SSDClean-SSDSolvePars [S,Alpha] --SSDClean-BICFactor 0 --Mask-Auto=1 --Mask-SigTh=10.00 --Selection-UVRangeKm=[5.0,2000.000000] --GAClean-MinSizeInit=10'.format(CONFIG['data']['mslist'], ic=CONFIG['image']['data_column'], cell=float(CONFIG['image']['cellsize_full'], beam=DDF_RESTORING_BEAM))
    LOGGER.info(CMD)
    subprocess.call(CMD, shell=True)

if os.path.exists(os.getcwd() + '/image_dirin_SSD_init_natural.app.restored.fits.mask.fits'):
    LOGGER.info('First mask already exists, not recreating.')
else:
    LOGGER.info('Creating mask from initial image.')
    CMD = 'MakeMask.py --RestoredIm=image_dirin_SSD_init_natural.app.restored.fits --Th=7.5 --Box=50,2'
    LOGGER.info(CMD)
    subprocess.call('MakeMask.py --RestoredIm=image_dirin_SSD_init_natural.app.restored.fits --Th=7.5 --Box=50,2', shell=True)

if os.path.exists(os.getcwd() + '/extended-mask-high.fits'):
    LOGGER.info('Extended emission mask already exists, not recreating.')
else:
    LOGGER.info('Creating extended emission mask from 6'' image.')
    make_extended_mask(infile=CONFIG['subtract']['lotss_directory'] + '/image_full_ampphase_di_m.NS.app.restored.fits', fullresfile='image_dirin_SSD_init_natural.app.restored.fits', sizethresh=250, rootname='extended')
    LOGGER.info('Extended emission mask saved as extended-mask-high.fits')
    LOGGER.info('Merging with image-based mask.'
    merge_mask(in1='extended-mask-high.fits', in2='image_dirin_SSD_init_natural.app.restored.fits.mask.fits', outfile='mask-merged-1.fits')

if os.path.exists(os.getcwd() + '/image_dirin_SSD_init_natural_m.int.restored.fits'):
    LOGGER.info('Mask-cleaned image already exists, not recreating.')
else:
    LOGGER.info('Cleaning deeper with mask.')
    CMD = 'DDF.py --Output-Name=image_dirin_SSD_init_natural_m --Data-MS={:s} --Deconv-PeakFactor 0.050000 --Data-ColName {ic:s} --Data-ChunkHours 4 --Parallel-NCPU=32 --Beam-CenterNorm=1 --Deconv-CycleFactor=0 --Deconv-MaxMinorIter=10000 --Deconv-MaxMajorIter=3 --Deconv-Mode SSD --Beam-Model=LOFAR --Beam-LOFARBeamMode=A --Weight-Mode Natural  --Image-NPix=25000 --CF-wmax 50000 --CF-Nw 100 --Output-Also onNeds --Image-Cell {cell:f} --Facets-NFacets=7 --SSDClean-NEnlargeData 0 --Freq-NDegridBand 1 --Beam-NBand 1 --Facets-DiamMax 1.5 --Facets-DiamMin 0.1 --Deconv-RMSFactor=3.000000 --SSDClean-ConvFFTSwitch 10000 --Data-Sort 1 --Cache-Dir=. --Log-Memory 1 --GAClean-RMSFactorInitHMP 1.000000 --GAClean-MaxMinorIterInitHMP 10000.000000 --DDESolutions-SolsDir=SOLSDIR --Cache-Weight=reset --Output-Mode=Clean --Output-RestoringBeam {beam:s} --Weight-ColName="IMAGING_WEIGHT" --Freq-NBand=2 --RIME-DecorrMode=FT --SSDClean-SSDSolvePars [S,Alpha] --SSDClean-BICFactor 0 --Mask-Auto=1 --Mask-SigTh=10.00 --Selection-UVRangeKm=[5.0,2000.000000] --GAClean-MinSizeInit=10 --Mask-External=mask-merged-1.fits --Predict-InitDicoModel=image_dirin_SSD_init_natural.DicoModel --Cache-Dirty=forceresidual'.format(CONFIG['data']['mslist'], ic=CONFIG['image']['data_column'], cell=float(CONFIG['image']['cellsize_full'], beam=DDF_RESTORING_BEAM))
    LOGGER.info(CMD)
    subprocess.call(CMD, shell=True)

LOGGER.info('Making PyBDSF catalogue of 1'' map.')

run_pybdsf(fitsname='image_dirin_SSD_init_natural_m.int.restored.fits', detectimage='image_dirin_SSD_init_natural_m.app.restored.fits')

IN_CATALOGUE = 'skymodel_1asec_lbregion_pybdsf.csv'
# https://stackoverflow.com/questions/16414410/delete-empty-lines-using-sed
subprocess.call("sed '/^[[:space:]]*$/d' {:s} > {:s}".format(IN_CATALOGUE, 'skymodel_1asec_lbregion_pybdsf.sedded.csv'), shell=True)

CATALOGUE = 'skymodel_1asec_lbregion_pybdsf.sedded.csv'
LOGGER.info('Wrote PyBDSF catalogue to {:s}'.format(CATALOGUE))

LOGGER.info('Selecting directions for DDE calibrators.')

if CONFIG['data'].getboolean('do_apply_kms'):
    PARSET = '''numthreads=4
    msout.storagemanager=dysco
    msout.writefullresflag = False

    steps=[explode]
    explode.steps=[shift,avg1,applykms,apply1,apply2,adder,filter,averager,msout]
    explode.replaceparms = [shift.phasecenter, msout.name]

    applykms.type = applycal
    applykms.solset = sol001
    applykms.steps = [ac_amp, ac_phase]
    applykms.ac_amp.correction = amplitude000
    applykms.ac_phase.correction = phase000
    '''
else:
    PARSET = '''numthreads=4
    msout.storagemanager=dysco
    msout.writefullresflag = False

    steps=[explode]
    explode.steps=[shift,avg1,apply1,apply2,adder,filter,averager,msout]
    explode.replaceparms = [shift.phasecenter, msout.name]
    '''

PARSET += '''shift.type=phaseshift
apply1.type = applycal
apply1.parmdb = /project/sksp/Data/L659948_4ch_4s/infield_calibrator/phaseonlySL333880_1ch_16s.mssolsgrid_8.h5
apply1.solset = sol001
apply1.correction = phase000

apply2.type = applycal
apply2.parmdb = /project/sksp/Data/L659948_4ch_4s/infield_calibrator/SL333880_1ch_16s.mssolsgrid_8.h5
apply2.solset = sol001
apply2.steps = [ac2_amp, ac2_phase]
apply2.ac2_amp.correction = amplitude000
apply2.ac2_phase.correction = phase000

adder.type=stationadder
adder.stations={ST001:'CS*'}

filter.type=filter
filter.baseline=^[C]S*&&
filter.remove=True

averager.type=averager
averager.freqstep=4
averager.timestep=15

msout.overwrite = True
'''

make_dde_directions(CATALOGUE, parset=PARSET)

# Now split out all directions.
# You should really do this on an SSD, otherwise it could take literal weeks.
LOGGER.info('[Not doing this] Splitting out DDE calibrators [not yet implemented]')
LOGGER.warning('NOTE: If you are not running on an SSD and/or (preferrably) in a distributed fashion, be prepared to wait a long time!')

if not os.path.isfile(CONFIG['solutions']['ddsols_h5']):
    die('DDE solutions not found!')
else:
    LOGGER.info('Creating final 1'' image with DD solutions.')
    CMD = 'DDF.py --Output-Name=image_dd --Data-MS={:s} --Deconv-PeakFactor 0.050000 --Data-ColName {ic:s} --Data-ChunkHours 4 --Parallel-NCPU=32 --Beam-CenterNorm=1 --Deconv-CycleFactor=0 --Deconv-MaxMinorIter=10000 --Deconv-MaxMajorIter=6 --Deconv-Mode SSD --Beam-Model=LOFAR --Beam-LOFARBeamMode=A --Weight-Mode Natural  --Image-NPix=25000 --CF-wmax 50000 --CF-Nw 100 --Output-Also onNeds --Image-Cell {cell:f} --Facets-NFacets=7 --SSDClean-NEnlargeData 0 --Freq-NDegridBand 1 --Beam-NBand 1 --Facets-DiamMax 0.5 --Facets-DiamMin 0.1 --Deconv-RMSFactor=3.000000 --SSDClean-ConvFFTSwitch 10000 --Data-Sort 1 --Cache-Dir=. --Log-Memory 1 --GAClean-RMSFactorInitHMP 1.000000 --GAClean-MaxMinorIterInitHMP 10000.000000 --DDESolutions-SolsDir=SOLSDIR --Cache-Weight=reset --Output-Mode=Clean --Output-RestoringBeam {beam:s} --Weight-ColName="IMAGING_WEIGHT" --Freq-NBand=2 --RIME-DecorrMode=FT --SSDClean-SSDSolvePars [S,Alpha] --SSDClean-BICFactor 0 --Mask-Auto=1 --Mask-SigTh=10.00 --Selection-UVRangeKm=[5.0,2000.000000] --GAClean-MinSizeInit=10 --Mask-External=image_dirin_SSD_init_natural_m.app.restored.fits.mask.fits --DDESolutions-DDSols={ddsols:s}'.format(CONFIG['data']['mslist'], ic=CONFIG['image']['data_column'], cell=float(CONFIG['image']['cellsize_full'], beam=DDF_RESTORING_BEAM), ddsols=CONFIG['solutions']['ddsols_h5'])
    LOGGER.info(CMD)
    subprocess.call(CMD, shell=True)

LOGGER.info('Making PyBDSF catalogue of DD calibrated 1'' map.')

run_pybdsf(fitsname='image_dirin_SSD_init_natural_m.int.restored.fits', detectimage='image_dirin_SSD_init_natural_m.app.restored.fits', outcat='catalogue_1asec_final_dd')

IN_CATALGOUE = 'catalogue_1asec_final_dd.csv'
subprocess.call("sed '/^[[:space:]]*$/d' {:s} > {:s}".format(IN_CATALOGUE, 'catalogue_1asec_final_dd.sedded.csv'), shell=True)

if not CONFIG['mosaic'].getboolean('do_mosaic'):
    LOGGER.info('Pipeline finished successfully.')
    sys.exit(0)

LOGGER.info('Creating directions for full FoV 0.3'' imaging.')
tab = ct.taql('SELECT REFERENCE_DIR FROM {:s}::FIELD'.format(MSES[0]))
PHASECENTER = tab.getcol('REFERENCE_DIR').squeeze()
make_tiles(*PHASECENTER)

# Make phaseshifted copies of each tile and image.
DPPP_PARSETS = sorted(glob.glob('shift_to_facet_*.parset'))
for i, p in enumerate(DPPP_PARSETS):
    for ms in MSES:
        subprocess.call('DPPP {:s} msin={:s} msout={:s}'.format(p, ms, ms[:-3] + '.facet_{:02d}'.format(i), shell=True)
    # Image here.
    # WSClean with IDG on GPU, or DDFacet?
