#!/usr/bin/python
# -*- coding: utf-8 -*-

################################################################################
#
#	RMG - Reaction Mechanism Generator
#
#	Copyright (c) 2002-2009 Prof. William H. Green (whgreen@mit.edu) and the
#	RMG Team (rmg_dev@mit.edu)
#
#	Permission is hereby granted, free of charge, to any person obtaining a
#	copy of this software and associated documentation files (the 'Software'),
#	to deal in the Software without restriction, including without limitation
#	the rights to use, copy, modify, merge, publish, distribute, sublicense,
#	and/or sell copies of the Software, and to permit persons to whom the
#	Software is furnished to do so, subject to the following conditions:
#
#	The above copyright notice and this permission notice shall be included in
#	all copies or substantial portions of the Software.
#
#	THE SOFTWARE IS PROVIDED 'AS IS', WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#	IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#	FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#	AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#	LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
#	FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
#	DEALINGS IN THE SOFTWARE.
#
################################################################################

"""
Contains classes for working with the reaction model generated by RMG.
"""

import logging
import math
import numpy
import scipy.integrate

import constants
import reaction

################################################################################

class ReactionModel:
	"""
	Represent a generic reaction model. A reaction model consists of `species`,
	a list of species, and `reactions`, a list of reactions.
	"""	

	def __init__(self, species=None, reactions=None):
		self.species = species or []
		self.reactions = reactions or []

################################################################################

class CoreEdgeReactionModel:
	"""
	Represent a reaction model constructed using a rate-based screening
	algorithm. The `core` is a reaction model that represents species and 
	reactions currently in the model, while the `edge` is a reaction model that
	represents species and reactions identified as candidates for addition to 
	the core.
	"""	

	def __init__(self, core=None, edge=None):
		if core is None:
			self.core = ReactionModel()
		else:
			self.core = core
		if edge is None:
			self.edge = ReactionModel()
		else:
			self.edge = edge
		self.fluxTolerance = 1.0
		self.absoluteTolerance = 1.0e-8
		self.relativeTolerance = 1.0e-4

	def initialize(self, coreSpecies):
		"""
		Initialize a reaction model with a list `coreSpecies` of species to
		start out with.
		"""

		logging.info('')
		
		for species1 in coreSpecies:
			# Generate reactions if reactive
			rxnList = []
			if species1.reactive:
				# Generate unimolecular reactions
				rxnList.extend(reaction.kineticsDatabase.getReactions([species1]))
				# Generate bimolecular reactions
				for species2 in self.core.species:
					rxnList.extend(reaction.kineticsDatabase.getReactions([species1, species2]))
			# Add to core
			self.addSpeciesToCore(species1)
			# Add to edge
			for rxn in rxnList:
				for spec in rxn.reactants:
					if spec not in self.edge.species and spec not in self.core.species:
						self.addSpeciesToEdge(spec)
				for spec in rxn.products:
					if spec not in self.edge.species and spec not in self.core.species:
						self.addSpeciesToEdge(spec)
				self.addReactionToEdge(rxn)

		logging.info('')
		logging.info('After core-edge reaction model initialization:')
		logging.info('\tThe model core has %s species and %s reactions' % (len(self.core.species), len(self.core.reactions)))
		logging.info('\tThe model edge has %s species and %s reactions' % (len(self.edge.species), len(self.edge.reactions)))
		logging.info('')
		
		# We cannot conduct simulations without having at least one reaction
		# in the core because otherwise we have no basis for selecting the
		# characteristic flux needed to test for model validity; thus we must
		# enlarge the reaction model until at least one reaction is in the core
		#while len(self.core.reactions) == 0:
		#	self.enlarge()

	def enlarge(self, newSpecies):
		"""
		Enlarge a reaction model by moving `newSpecies` from the edge to the
		core.
		"""

		rxnList = []
		rxnList.extend(reaction.kineticsDatabase.getReactions([newSpecies]))
		for coreSpecies in self.core.species:
			if coreSpecies.reactive:
				rxnList.extend(reaction.kineticsDatabase.getReactions([newSpecies, coreSpecies]))

		# Add new species
		self.addSpeciesToCore(newSpecies)

		# Add new reactions
		for rxn in rxnList:
			allSpeciesInCore = True
			for spec in rxn.reactants:
				if spec not in self.core.species: allSpeciesInCore = False
				if spec not in self.edge.species and spec not in self.core.species:
					self.addSpeciesToEdge(spec)
			for spec in rxn.products:
				if spec not in self.core.species: allSpeciesInCore = False
				if spec not in self.edge.species and spec not in self.core.species:
					self.addSpeciesToEdge(spec)
			if allSpeciesInCore:
				self.addReactionToCore(rxn)
			else:
				self.addReactionToEdge(rxn)

		logging.info('')
		logging.info('After model enlargement:')
		logging.info('\tThe model core has %s species and %s reactions' % (len(self.core.species), len(self.core.reactions)))
		logging.info('\tThe model edge has %s species and %s reactions' % (len(self.edge.species), len(self.edge.reactions)))
		logging.info('')

	def addSpeciesToCore(self, spec):
		"""
		Add a species `spec` to the reaction model core (and remove from edge if
		necessary). This function also moves any reactions in the edge that gain
		core status as a result of this change in status to the core.
		"""

		# Add the species to the core
		self.core.species.append(spec)

		if spec in self.edge.species:

			# If species was in edge, remove it
			self.edge.species.remove(spec)

			# Search edge for reactions that now contain only core species;
			# these belong in the model core and will be moved there
			rxnList = []
			for rxn in self.edge.reactions:
				allCore = True
				for reactant in rxn.reactants:
					if reactant not in self.core.species: allCore = False
				for product in rxn.products:
					if product not in self.core.species: allCore = False
				if allCore: rxnList.append(rxn)

			# Move any identified reactions to the core
			for rxn in rxnList:
				self.addReactionToCore(rxn)


	def addSpeciesToEdge(self, spec):
		"""
		Add a species `spec` to the reaction model edge.
		"""
		self.edge.species.append(spec)

	def addReactionToCore(self, rxn):
		"""
		Add a reaction `rxn` to the reaction model core (and remove from edge if
		necessary). This function assumes `rxn` has already been checked to
		ensure it is supposed to be a core reaction (i.e. all of its reactants
		AND all of its products are in the list of core species).
		"""
		self.core.reactions.append(rxn)
		if rxn in self.edge.reactions:
			self.edge.reactions.remove(rxn)

	def addReactionToEdge(self, rxn):
		"""
		Add a reaction `rxn` to the reaction model edge. This function assumes
		`rxn` has already been checked to ensure it is supposed to be an edge
		reaction (i.e. all of its reactants OR all of its products are in the
		list of core species, and the others are in either the core or the
		edge).
		"""
		self.edge.reactions.append(rxn)

	def isValid(self, T, P, conc):
		"""
		Return :data:`True` if the model is valid at the specified conditions -
		temperature `T`, pressure `P`, and dictionary of concentrations `conc` -
		or :data:`False` otherwise. A model is considered valid if the flux to
		all species in the edge is less than a certain tolerance (usually some
		fraction of the root mean square flux of all core reactions).
		"""

		# Determine the reaction fluxes
		rxnFlux = {}
		for rxn in self.core.reactions:
			rxnFlux[rxn] = rxn.getRate(T, P, conc)

		# Get the chracteristic flux to use for assessing model validity
		charFlux = self.fluxTolerance * math.sqrt(sum([flux**2 for flux in rxnFlux.values()]))

		# Determine the species fluxes (for edge species only)
		specFlux = {}
		for rxn in self.core.reactions:
			for reactant in rxn.reactants:
				if reactant in self.edge.species:
					try:
						specFlux[reactant] -= rxnFlux[rxn]
					except KeyError:
						specFlux[reactant] = -rxnFlux[rxn]
			for product in rxn.products:
				if product in self.edge.species:
					try:
						specFlux[product] += rxnFlux[rxn]
					except KeyError:
						specFlux[product] = rxnFlux[rxn]

		# Get maximum edge species flux
		maxSpecFlux, maxSpec = max([(specFlux[x],x) for x in specFlux])
		
		# If maximum edge species flux is greater than the tolerance, return
		# False and the species with the maximum flux
		if maxSpecFlux > charFlux:
			return False

		# At this stage the model has passed all validity tests and is therefore
		# presumed valid
		return True

	def getLists(self):
		"""
		Return lists of all of the species and reactions in the core and the
		edge.
		"""
		speciesList = []
		speciesList.extend(self.core.species)
		speciesList.extend(self.edge.species)
		reactionList = []
		reactionList.extend(self.core.reactions)
		reactionList.extend(self.edge.reactions)
		return speciesList, reactionList

	def getReactionRates(self, T, P, Ci):
		"""
		Return an array of reaction rates for each reaction in the model core
		and edge. The core reactions occupy the first rows of the array, while
		the edge reactions occupy the last rows.
		"""
		speciesList, reactionList = self.getLists()
		rxnRate = numpy.zeros(len(reactionList), float)
		for j, rxn in enumerate(reactionList):
			rxnRate[j] = rxn.getRate(T, P, Ci)
		return rxnRate

################################################################################

class TemperatureModel:
	"""
	Represent a temperature profile. Currently the only implemented model is
	isothermal (constant temperature).
	"""

	def __init__(self):
		self.type = ''
		self.temperatures = []
		
	def isIsothermal(self):
		return self.type == 'isothermal'
	
	def setIsothermal(self, temperature):
		self.type = 'isothermal'
		self.temperatures = [ [0.0, temperature] ]
	
	def getTemperature(self, time):
		if self.isIsothermal():
			return self.temperatures[0][1]
		else:
			return None
	
	def __str__(self):
		string = 'Temperature model: ' + self.type + ' '
		if self.isIsothermal():
			string += str(self.getTemperature(0))
		return string
	
################################################################################

class PressureModel:
	"""
	Represent a pressure profile. Currently the only implemented model is
	isobaric (constant pressure).
	"""
	
	def __init__(self):
		self.type = ''
		self.pressures = []
		
	def isIsobaric(self):
		return self.type == 'isobaric'
	
	def setIsobaric(self, pressure):
		self.type = 'isobaric'
		self.pressures = [ [0.0, pressure] ]
	
	def getPressure(self, time):
		if self.isIsobaric():
			return self.pressures[0][1]
		else:
			return None

	def __str__(self):
		string = 'Pressure model: ' + self.type + ' '
		if self.isIsobaric():
			string += str(self.getPressure(0))
		return string

################################################################################

class IdealGas:
	"""
	An equation of state based on the ideal gas approximation

	.. math::

		f(P, V, T, \\mathbf{N}) = NRT - PV

	where :math:`N \\equiv \\sum_i N_i` is the total number of moles.

	The ideal gas approximation is generally valid for gases at low pressures
	and moderate tempertaures; it does not predict the gas-liquid phase
	transition and is not applicable to liquids.
	"""

	def getTemperature(self, P, V, N):
		"""
		Return the temperature associated with pressure `P`, volume `V`, and
		numbers of moles `N`.
		"""
		return P * V / (sum(N) * constants.R)

	def getPressure(self, T, V, N):
		"""
		Return the temperature associated with temperature `T`, volume `V`, and
		numbers of moles `N`.
		"""
		return sum(N) * constants.R * T / V

	def getVolume(self, T, P, N):
		"""
		Return the volume associated with temperature `T`, pressure `P`, and
		numbers of moles `N`.
		"""
		return sum(N) * constants.R * T / P

	def getdPdV(self, P, V, T, N):
		"""
		Return the derivative :math:`\\frac{dP}{dV}\\bigg|_{T,\mathbf{N}}`
		evaluated at a given pressure `P`, volume `V`, temperature `T`, and
		numbers of moles `N`.
		"""
		return (-P/V)

	def getdPdT(self, P, V, T, N):
		"""
		Return the derivative :math:`\\frac{dP}{dT}\\bigg|_{V,\mathbf{N}}`
		evaluated at a given pressure `P`, volume `V`, temperature `T`, and
		numbers of moles `N`.
		"""
		return (P/T)

	def getdVdT(self, P, V, T, N):
		"""
		Return the derivative :math:`\\frac{dV}{dT}\\bigg|_{P,\mathbf{N}}`
		evaluated at a given pressure `P`, volume `V`, temperature `T`, and
		numbers of moles `N`.
		"""
		return (V/T)

	def getdVdP(self, P, V, T, N):
		"""
		Return the derivative :math:`\\frac{dV}{dP}\\bigg|_{T,\mathbf{N}}`
		evaluated at a given pressure `P`, volume `V`, temperature `T`, and
		numbers of moles `N`.
		"""
		return 1.0 / self.getdPdV(P, V, T, N)

	def getdTdP(self, P, V, T, N):
		"""
		Return the derivative :math:`\\frac{dT}{dP}\\bigg|_{V,\mathbf{N}}`
		evaluated at a given pressure `P`, volume `V`, temperature `T`, and
		numbers of moles `N`.
		"""
		return 1.0 / self.getdPdT(P, V, T, N)

	def getdTdV(self, P, V, T, N):
		"""
		Return the derivative :math:`\\frac{dT}{dV}\\bigg|_{P,\mathbf{N}}`
		evaluated at a given pressure `P`, volume `V`, temperature `T`, and
		numbers of moles `N`.
		"""
		return 1.0 / self.getdVdT(P, V, T, N)

	def getdPdNi(self, P, V, T, N, i):
		"""
		Return the derivative :math:`\\frac{dP}{dN_i}\\bigg|_{T, V,\mathbf{N}_{j \\ne i}}`
		evaluated at a given pressure `P`, volume `V`, temperature `T`, and
		numbers of moles `N`. The final parameter `i` is used to determine which
		species to use; if `N` is a list, then `i` is an index, while if `N` is
		a dictionary, `i` is a key.
		"""
		if type(N) is dict: return (P/numpy.sum(N.values()))
		else: return (P/numpy.sum(N))

	def getdVdNi(self, P, V, T, N, i):
		"""
		Return the derivative :math:`\\frac{dV}{dN_i}\\bigg|_{T, P,\mathbf{N}_{j \\ne i}}`
		evaluated at a given pressure `P`, volume `V`, temperature `T`, and
		numbers of moles `N`. The final parameter `i` is the index of the
		species of interest, corresponding to an index into the list `N`.
		"""
		if type(N) is dict: return (V/numpy.sum(N.values()))
		else: return (V/numpy.sum(N))

	def getdTdNi(self, P, V, T, N, i):
		"""
		Return the derivative :math:`\\frac{dT}{dN_i}\\bigg|_{P, V,\mathbf{N}_{j \\ne i}}`
		evaluated at a given pressure `P`, volume `V`, temperature `T`, and
		numbers of moles `N`. The final parameter `i` is the index of the
		species of interest, corresponding to an index into the list `N`.
		"""
		if type(N) is dict: return (-T/numpy.sum(N.values()))
		else: return (-T/numpy.sum(N))

################################################################################

class InvalidReactionSystemException(Exception):
	"""
	An exception used when an invalid reaction system is encountered.
	"""

	def __init__(self, label):
		self.label = label

	def __str__(self):
		return 'Invalid reaction system: ' + self.label

################################################################################

class ReactionSystem:
	"""
	Represent a generic reaction system, e.g. a chemical reactor. A reaction
	system is defined by a temperature model `temperatureModel`, a pressure 
	model `pressureModel`, a volume model `volumeModel`, and a dictionary of 
	initial and constant concentrations `initialConcentration`. Only two of
	`temperatureModel`, `pressureModel`, and `volumeModel` are independent; the
	remaining one must be set to :data:`None`.

	Each RMG job can handle multiple reaction systems; the resulting model
	will generally be the union of the models that would have been generated 
	via individual RMG jobs, and will therefore be valid across all reaction
	systems provided.	
	"""

	def __init__(self, temperatureModel=None, pressureModel=None, 
				 volumeModel=None, initialConcentration=None):
		self.setModels(temperatureModel, pressureModel, volumeModel)
		self.initialConcentration = initialConcentration or {}

	def setModels(self, temperatureModel, pressureModel, volumeModel):
		"""
		Set any two of the reactor's `temperatureModel`, `pressureModel` or
		`volumeModel`. Attempting to set all three to non-None will cause an
		:class:`InvalidReactorModelException` to be raised.
		"""
		# Fail if all three models are specified
		if temperatureModel is not None and pressureModel is not None and volumeModel is not None:
			raise InvalidReactionSystemException('Attempted to specify temperature, pressure, and volume models; can only specify two of these at a time.')
		# Otherwise set models
		self.temperatureModel = temperatureModel
		self.pressureModel = pressureModel
		self.volumeModel = volumeModel

################################################################################

class BatchReactor(ReactionSystem):
	"""
	A model of a batch reactor. A batch reactor is a well-mixed system with
	no external inputs or output, so all transport effects can be neglected.
	Any two of a temperature model, pressure model, and volume model can be
	specified; the remaining one is dependent on the choice of the other two.
	"""

	def __init__(self, temperatureModel=None, pressureModel=None, \
				 volumeModel=None, initialConcentration=None):
		ReactionSystem.__init__(self, temperatureModel, pressureModel, \
				volumeModel, initialConcentration)

	def getResidual(self, y, t, model, stoichiometry):
		"""
		Return the residual function for this reactor model, evaluated at
		time `t` and values of the dependent variables `y`. The residual
		function is the right-hand side of the equation

		.. math:: \\frac{d \\mathbf{y}}{dt} = \\mathbf{R}(\\mathbf{y})

		The dependent variables include temperature, pressure, volume, and
		numbers of moles for each species.
		"""

		P, V, T = y[0:3]; Ni = y[3:]
		
		# Reaction rates
		rxnRate = self.getReactionRates(P, V, T, Ni, model)
		
		# Species balances
		dNidt = numpy.dot(stoichiometry[0:len(model.core.species), 0:len(model.core.reactions)], rxnRate[0:len(model.core.reactions)])

		# Energy balance (assume isothermal for now)
		dTdt = 0.0

		# Pressure balance (assume isobaric for now)
		dPdt = 0.0

		# Volume balance comes from equation of state
		dVdP = self.equationOfState.getdVdP(P, V, T, Ni)
		dVdT = self.equationOfState.getdVdT(P, V, T, Ni)
		dVdNi = numpy.array([self.equationOfState.getdVdNi(P, V, T, Ni, i) for i in Ni])
		dVdt = dVdP * dPdt + dVdT * dTdt + numpy.dot(dVdNi, dNidt)
		
		# Convert derivatives to residual
		dydt = numpy.zeros(len(dNidt)+3, float)
		dydt[0] = dPdt
		dydt[1] = dVdt
		dydt[2] = dTdt
		dydt[3:] = dNidt

		# Return residual
		return dydt

	def getReactionRates(self, P, V, T, Ni, model):
		"""
		Evaluate the reaction rates for all reactions in the model (core and
		edge).
		"""

		Ci = {}
		for i, spec in enumerate(model.core.species):
			Ci[spec] = Ni[i] / V

		return model.getReactionRates(T, P, Ci)

	def isModelValid(self, model, P, V, T, Ni, stoichiometry, t):
		"""
		Returns :data:`True` if `model` is valid at the specified pressure
		`P`, volume `V`, temperature `T`, and numbers of moles `Ni`. The final
		parameter `t` is the current simulation time.
		"""

		speciesList, reactionList = model.getLists()

		rxnRates = self.getReactionRates(P, V, T, Ni, model)

		charFlux = model.fluxTolerance * math.sqrt(sum([x**2 for x in rxnRates[0:len(model.core.reactions)]]))
		dNidt = numpy.dot(stoichiometry, rxnRates)
		maxSpeciesFlux, maxSpecies = max([ (value, i+len(model.core.species)) for i, value in enumerate(dNidt[len(model.core.species):]) ])
		if maxSpeciesFlux > charFlux:
			logging.info('At t = %s, the species flux for %s exceeds the characteristic flux' % (t, speciesList[maxSpecies]))
			logging.info('\tCharacteristic flux: %s' % (charFlux))
			logging.info('\tSpecies flux for %s: %s ' % (speciesList[maxSpecies], maxSpeciesFlux))
			logging.info('')
			return False, speciesList[maxSpecies]

		return True, None

	def simulate(self, model):
		"""
		Conduct a simulation of the current reaction system using the core-edge
		reaction model `model`.
		"""

		# Assemble stoichiometry matrix for all core and edge species
		# Rows are species (core, then edge); columns are reactions (core, then edge)
		speciesList, reactionList = model.getLists()
		stoichiometry = numpy.zeros((len(speciesList), len(reactionList)), float)
		for j, rxn in enumerate(reactionList):
			for i, spec in enumerate(speciesList):
				stoichiometry[i,j] = rxn.getStoichiometricCoefficient(spec)

		# Set up initial conditions
		P = float(self.pressureModel.getPressure(0))
		T = float(self.temperatureModel.getTemperature(0))
		V = 1.0 # [=] m**3
		Ni = numpy.zeros(len(model.core.species), float)
		for i, spec in enumerate(model.core.species):
			if spec in self.initialConcentration:
				Ni[i] = self.initialConcentration[spec] * V
		
		# Test for model validity
		valid, newSpecies = self.isModelValid(model, P, V, T, Ni, stoichiometry, 0.0)
		if not valid:
			return False, newSpecies

		t0 = 1e-20; tf = 1e-20 * 1.1
		y = [P, V, T]; y.extend(Ni)
		y0 = y
		while t0 < 1.0e0:

			# Conduct integration
			y, info = scipy.integrate.odeint(self.getResidual, y, (t0, tf), \
				args=(model, stoichiometry), atol=model.absoluteTolerance, \
				rtol=model.relativeTolerance, full_output=True)
			y = y[-1]
			P, V, T = y[0:3]; Ni = y[3:]
			print tf, P, V, T, Ni
			
			# Test for model validity
			valid, newSpecies = self.isModelValid(model, P, V, T, Ni, stoichiometry, tf)
			if not valid:
				return False, newSpecies

			# Test for simulation completion
			if y[3] < 0.1 * y0[3]:
				return True, None

			# Prepare for next integration
			t0 = tf
			tf = info['tcur'][-1] * 1.1

		return True, None

	
################################################################################

if __name__ == '__main__':

	import chem
	import data
	import species
	import reaction

	import os.path
	import main
	main.initializeLog(logging.DEBUG)

	datapath = '../data/RMG_database/'

	logging.debug('General database: ' + os.path.abspath(datapath))
	species.thermoDatabase = species.ThermoDatabaseSet()
	species.thermoDatabase.load(datapath)
	species.forbiddenStructures = data.Dictionary()
	species.forbiddenStructures.load(datapath + 'forbiddenStructure.txt')
	species.forbiddenStructures.toStructure()
	#reaction.kineticsDatabase = reaction.ReactionFamilySet()
	#reaction.kineticsDatabase.load(datapath)

	structure = chem.Structure(); structure.fromSMILES('C')
	CH4 = species.makeNewSpecies(structure)

	structure = chem.Structure(); structure.fromSMILES('[H]')
	H = species.makeNewSpecies(structure)

	structure = chem.Structure(); structure.fromSMILES('[CH3]')
	CH3 = species.makeNewSpecies(structure)

	forward = reaction.Reaction([CH3, H], [CH4])
	reverse = reaction.Reaction([CH4], [CH3, H])
	forward.reverse = reverse
	reverse.reverse = forward

	kinetics = reaction.ArrheniusEPKinetics()
	kinetics.fromDatabase([300, 2000, 1.93E14, 0, 0, 0.27, 0, 0, 0, 0, 3], '', 2)
	forward.kinetics = [kinetics]

	speciesList = [CH3, H, CH4]
	reactionList = [forward]

	reactionSystem = BatchReactor()
	reactionSystem.temperatureModel = TemperatureModel()
	reactionSystem.temperatureModel.setIsothermal(pq.Quantity(1000, 'K'))
	reactionSystem.pressureModel = PressureModel()
	reactionSystem.pressureModel.setIsobaric(pq.Quantity(1, 'bar'))
	reactionSystem.equationOfState = IdealGas()
	reactionSystem.initialConcentration[CH4] = pq.Quantity(1, 'mol/m**3')

	reactionSystem.solve(0.0, 1.0e0, speciesList, reactionList)
	
	