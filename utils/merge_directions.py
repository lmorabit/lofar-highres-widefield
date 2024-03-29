import argparse
import glob
import sys

from losoto.lib_operations import reorderAxes
from scipy.interpolate import interp1d

import casacore.tables as ct
import losoto.h5parm as h5parm
import numpy as np


def interp_along_axis(x, interp_from, interp_to, axis):
    print('Inter/extrapolating from {:d} to {:d} along axis {:d}'.format(len(interp_from), len(interp_to), axis))
    interp_vals = interp1d(interp_from, x, axis=axis, kind='nearest', fill_value='extrapolate')
    new_vals = interp_vals(interp_to)
    return new_vals

parser =argparse.ArgumentParser()
parser.add_argument('--mspath', dest='msdir', help='Path to the directory with easurement sets to pull frequency axis from, when converting TEC to phase.')
parser.add_argument('--mssuffix', dest='mssuffix', default='ms', help='Suffix of your measurement sets, e.g. MS or ms.')
parser.add_argument('--h5parms', dest='h5parms', nargs='+', help='Input H5parms to merge as directions, where each h5parm is one direction.')
parser.add_argument('--soltab', dest='soltab2merge', help='SolTab of the H5parms to merge.')
parser.add_argument('--solset-in', dest='solsetin', help='SolSet to take the soltab from.')
parser.add_argument('--h5parm-out', dest='h5out', help='Output H5parm with all directions present.')
parser.add_argument('--convert-tec', dest='convert_tec', action='store_true', default=False, help='Convert TEC values to their corresponding phase corrections base on the frequencies in the Measurement Sets.')
parser.add_argument('--append-to-solset', dest='append_to_solset', default='', help='Append the new soltab to the given solset instead of creating a new one.')
args = parser.parse_args()
convert_tec = args.convert_tec

mslist = sorted(glob.glob(args.msdir + '/*.' + args.mssuffix))
ms_first = mslist[0]
ms_last = mslist[-1]

print('Determining time axis...')
len_time_old = 0
ax_time = None
for ih5 in args.h5parms:
    th5 = h5parm.h5parm(ih5)
    ss = th5.getSolset('sol000')
    if 'tec000' in ss.getSoltabNames():
        st = ss.getSoltab('tec000')
    elif 'phase000' in ss.getSoltabNames():
        st = ss.getSoltab('phase000')
    ax_time_temp = st.getAxisValues('time')
    if len(ax_time_temp) > len_time_old:
        # Longer time axis meas a shorter solution interval was used.
        ax_time = ax_time_temp
        len_time_old = len(ax_time)
        name_time = ih5
    th5.close()
print('Fastest time axis taken from {:s} with a solution interval of {:f} s.'.format(name_time, ax_time[1]-ax_time[0]))

h5 = h5parm.h5parm(args.h5parms[0])
ss = h5.getSolset(args.solsetin)
st = ss.getSoltab(args.soltab2merge)

antennas = st.getAxisValues('ant')
ss_antennas = ss.obj.antenna.read()
directions = []

vals = st.getValues()[0]
AN = st.getAxesNames()
axes_new = ['ant', 'freq', 'time']
if 'dir' in AN:
    axes_new = ['dir'] + axes_new
if 'pol' in AN:
    axes_new = ['pol'] + axes_new
    polarizations = st.getAxisValues('pol')

vals_reordered = reorderAxes(vals, st.getAxesNames(), axes_new)

if 'phase' in args.soltab2merge:
    print('Determining frequency grid...')
    len_freq_old = 0
    ax_freq = None
    for ih5 in args.h5parms:
        fh5 = h5parm.h5parm(ih5)
        ss = fh5.getSolset('sol000')
        if 'tec000' in ss.getSoltabNames():
            st = ss.getSoltab('tec000')
        elif 'phase000' in ss.getSoltabNames():
            st = ss.getSoltab('phase000')
        ax_freq_temp = st.getAxisValues('freq')
        if len(ax_freq_temp) > len_freq_old:
            # Longer freq axis meas a shorter solution interval was used.
            ax_freq = ax_freq_temp
            len_freq_old = len(ax_freq)
            name_freq = ih5
        fh5.close()
    print('Fastest frequency axis taken from {:s} with a solution interval of {:f} Hz.'.format(name_freq, ax_freq[1]-ax_freq[0]))
    if 'pol' in axes_new:
        phases = np.zeros((len(polarizations), 1, len(antennas), len(ax_freq), len(ax_time)))
    else:
        phases = np.zeros((1, len(antennas), len(ax_freq), len(ax_time)))
elif convert_tec and 'tec' in args.soltab2merge:
    print('Determining frequency grid...')
    ff = ct.taql('SELECT CHAN_FREQ, CHAN_WIDTH FROM ' + ms_first + '::SPECTRAL_WINDOW')
    freq_first = ff.getcol('CHAN_FREQ')[0][0]
    freq_spacing = ff.getcol('CHAN_WIDTH')[0][0]
    ff.close()

    fl = ct.taql('SELECT CHAN_FREQ, CHAN_WIDTH FROM ' + ms_last + '::SPECTRAL_WINDOW')
    freq_last = fl.getcol('CHAN_FREQ')[0][0]
    print(freq_first, freq_last, freq_spacing)
    ax_freq = np.arange(freq_first, freq_last + freq_spacing, freq_spacing)
    phases = np.zeros((1, 1, len(antennas), len(ax_freq), len(ax_time)))
    print('Frequency axis taken Measurement Sets with a solution interval of {:f} Hz.'.format(ax_freq[1]-ax_freq[0]))
elif not convert_tec:
    phases = np.zeros(vals_reordered.shape)

h5out = h5parm.h5parm(args.h5out, readonly=False)
if args.append_to_solset:
    solsetout = h5out.getSolset(append_to_solset)
else:
    # Try to make a new one.
    solsetout = h5out.makeSolset('sol000')
antennasout = solsetout.getAnt()
antennatable = solsetout.obj._f_get_child('antenna')
antennatable.append(ss_antennas)
sourcelist = []

for i, h5 in enumerate(args.h5parms):
    print('Processing direction for ' + h5)
    # Read in the data
    h5 = h5parm.h5parm(h5)
    ss = h5.getSolset(args.solsetin)
    st = ss.getSoltab(args.soltab2merge)
    d = ss.getSou()
    source_coords = d[d.keys()[0]]
    d = 'Dir{:02d}'.format(i)
    if (d, source_coords) not in sourcelist:
        print('Adding new direction {:f},{:f}'.format(*source_coords))
        idx = i
        directions.append(d)
        sourcelist.append((d, source_coords))
    else:
        # Direction already exists, add to the existing solutions.
        print('Direction {:f},{:f} already exists, adding solutions instead.'.format(*source_coords))
        idx = directions.index(d)
        d = 'Dir{:02d}'.format(idx)
    if st.getType() == 'tec' and convert_tec:
        # Convert tec to phase.
        tec_tmp = st.getValues()[0]
        #tec = reorderAxes(tec_tmp, st.getAxesNames(), ['time', 'freq', 'ant', 'dir'])
        tec = reorderAxes(tec_tmp, st.getAxesNames(), axes_new)
        # -1 assumes the expected shape along the frequency axis.
        freqs = ax_freq.reshape(1, 1, 1, -1, 1)
        tecphase = (-8.4479745e9 * tec / freqs)
        tp = interp_along_axis(tecphase, st.getAxisValues('time'), ax_time, -1)
        # Now add the phases to the total phase correction for this direction.
        if idx == 0:
            phases[:, idx, :, :, :] += tp[:, 0, :, :, :]
        elif idx > 0:
            phases = np.append(phases, tp, axis=1)
    elif st.getType() == 'tec' and not convert_tec:
        tec_tmp = st.getValues()[0]
        if 'dir' in axes_new:
            # TEC will never have a polarization axis and has only one frequency, so the latter indexing can stay: first direction, all antennas, first frequency, all times.
            tec = reorderAxes(tec_tmp, st.getAxesNames(), axes_new)[0, :, 0, :]
        else:
            tec = reorderAxes(tec_tmp, st.getAxesNames(), axes_new)
        # -1 assumes the expected shape along that axis.
        print(axes_new)
        tp = interp_along_axis(tec, st.getAxisValues('time'), ax_time, -1)
        tp = tp.reshape(1, tp.shape[0], 1, tp.shape[1])
        # Now add the tecs to the total phase correction for this direction.
        if idx == 0:
            # Axis order is dir,ant,time.
            # Set the first direction.
            print(phases.shape)
            print(phases[idx, :, :].shape)
            print(tp.shape)
            if 'dir' in axes_new:
                phases[idx, :, :, :] += tp[0, ...]
            else:
                phases[idx, :, :] += tp
        elif idx > 0:
            phases = np.append(phases, tp, axis=0)
    elif st.getType() == 'phase':
        phase_tmp = st.getValues()[0]
        phase = reorderAxes(phase_tmp, st.getAxesNames(), axes_new)
        tp = interp_along_axis(phase, st.getAxisValues('time'), ax_time, -1)
        tp = interp_along_axis(phase, st.getAxisValues('freq'), ax_freq, -2)
        tp = tp.reshape(tp.shape[0], -1, *tp.shape[1:])
        # Now add the phases to the total phase correction for this direction.
        if idx == 0:
            if 'dir' in axes_new:
                phases[:, idx, :, :, :] += tp[:, 0, ...]
            else:
                phases += tp
        else:
            if 'pol' in axes_new:
                phases = np.append(phases, tp, axis=1)
            else:
                phases = np.append(phases, tp, axis=0)
            
    h5.close()

# Create the output h5parm.
weights = np.ones(phases.shape)
sources = np.array(sourcelist, dtype=[('name', 'S128'), ('dir', '<f4', (2,))])
solsetout.obj.source.append(sources)
if 'phase' in args.soltab2merge and len(polarizations) > 0:
    solsetout.makeSoltab('phase', axesNames=axes_new, axesVals=[polarizations, directions, antennas, ax_freq, ax_time], vals=phases, weights=weights)
elif 'phase' in args.soltab2merge and len(polarizations) == 0:
    weights = np.ones(phases[0,...].shape)
    solsetout.makeSoltab('phase', axesNames=axes_new, axesVals=[directions, antennas, ax_freq, ax_time], vals=phases[0,...], weights=weights)
if 'tec' in args.soltab2merge:
    if not convert_tec:
        weights = np.ones(phases[:, :, 0, :].shape)
        solsetout.makeSoltab('tec', axesNames=['dir', 'ant', 'time'], axesVals=[directions, antennas, ax_time], vals=phases[:, :, 0, :], weights=weights)
    elif convert_tec:
        weights = np.ones(phases[0,...].shape)
        #solsetout.makeSoltab('phase', axesNames=['pol', 'dir', 'ant', 'freq', 'time'], axesVals=[['XX'], directions, antennas, ax_freq, ax_time], vals=phases, weights=weights)
        solsetout.makeSoltab('phase', axesNames=['dir', 'ant', 'freq', 'time'], axesVals=[directions, antennas, ax_freq, ax_time], vals=phases[0,...], weights=weights)
h5out.close()
