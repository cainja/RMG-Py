import os
import re
import logging
import external.cclib.parser
import time
from subprocess import Popen
from copy import deepcopy
import numpy
import shutil
from rmgpy.molecule import Molecule
from rmgpy.species import Species, TransitionState
from rmgpy.kinetics import Wigner
from molecule import QMMolecule, Geometry
from rmgpy.cantherm.gaussian import GaussianLog
from rmgpy.cantherm.kinetics import KineticsJob
import symmetry

try:
    import rdkit
    from rdkit import DistanceGeometry
    from rdkit.Chem.Pharm3D import EmbedLib
except ImportError:
    logging.info("To use transition state searches, you must correctly install rdkit")

def matrixToString(matrix):
    """Returns a string representation of a matrix, for printing to the console"""
    text = '\n'.join([ ' '.join([str(round(item, 1)) for item in line]) for line in matrix ])
    return text.replace('1000.0', '1e3')

                
class QMReaction:
    
    # file_store_path = 'QMfiles'
    # if not os.path.exists(file_store_path):
    #     logging.info("Creating directory %s for mol files."%os.path.abspath(file_store_path))
    #     os.makedirs(file_store_path)
    
    def __init__(self, reaction, settings):
        self.reaction = reaction
        self.settings = settings
        
        if isinstance(self.reaction.reactants[0], Molecule):
            reactants = sorted([s.toSMILES() for s in self.reaction.reactants])
            products = sorted([s.toSMILES() for s in self.reaction.products])
        elif isinstance(self.reaction.reactants[0], Species):
            reactants = sorted([s.molecule[0].toSMILES() for s in self.reaction.reactants])
            products = sorted([s.molecule[0].toSMILES() for s in self.reaction.products])
        stringID = "+".join(reactants) + "_" + "+".join(products)
        
        self.uniqueID = stringID
        
        self.geometry = None
        self.transitionState = None
    
    def getFilePath(self, extension):
        """
        Should return the path to the file with the given extension.
        
        The provided extension should include the leading dot.
        
        Need to define some reaction line notation.
        Possibly '<Reaction_Family>/<reactant1SMILES>+<reactant2SMILES>--<product1SMILES>+<product2SMILES>' ???
        """
        return os.path.join(self.settings.fileStore, self.uniqueID  + extension)
    
    @property
    def outputFilePath(self):
        """Get the output file name."""
        return self.getFilePath(self.outputFileExtension)
    
    @property
    def inputFilePath(self):
        """Get the input file name."""
        return self.getFilePath(self.inputFileExtension)
    
    @property
    def ircOutputFilePath(self):
        """Get the irc output file name."""
        return self.getFilePath('IRC' + self.outputFileExtension)
    
    @property
    def ircInputFilePath(self):
        """Get the irc input file name."""
        return self.getFilePath('IRC' + self.inputFileExtension)
        
    def fixSortLabel(self, molecule):
        """
        This may not be required anymore. Was needed as when molecules were created, the
        rmg sorting labels would be set after where we tried to generate the TS.
        """
        sortLbl = 0
        for vertex in molecule.vertices:
            vertex.sortingLabel = sortLbl
            sortLbl += 1
        return molecule
    
    def getGeometry(self, molecule, settings):
        
        geom = Geometry(settings, molecule.toAugmentedInChIKey(), molecule)
        
        return geom
        
    def getRDKitMol(self, geometry):
        """
        Check there is no RDKit mol file already made. If so, use rdkit to make a rdmol from
        a mol file. If not, make rdmol from geometry.
        """ 
        geometry.generateRDKitGeometries()
        rdKitMol = rdkit.Chem.MolFromMolFile(geometry.getCrudeMolFilePath(), removeHs=False)      
        
        return rdKitMol
        
    def generateBoundsMatrix(self, molecule):
        """
        Uses rdkit to generate the bounds matrix of a rdkit molecule.
        """
        geometry = self.getGeometry(molecule, self.settings)
        rdKitMol = self.getRDKitMol(geometry)
        boundsMatrix = rdkit.Chem.rdDistGeom.GetMoleculeBoundsMatrix(rdKitMol)
        
        return rdKitMol, boundsMatrix, geometry
    
    def setLimits(self, bm, lbl1, lbl2, value, uncertainty):
        if lbl1 > lbl2:
            bm[lbl2][lbl1] = value + uncertainty/2
            bm[lbl1][lbl2] = max(0,value - uncertainty/2)
        else:
            bm[lbl2][lbl1] = max(0,value - uncertainty/2)
            bm[lbl1][lbl2] = value + uncertainty/2
    
        return bm
    
    def bmPreEdit(self, bm, sect):
        """
        Clean up some of the atom distance limits before attempting triangle smoothing.
        This ensures any edits made do not lead to unsolvable scenarios for the molecular
        embedding algorithm.
        
        sect is the list of atom indices belonging to one species.
        """
        others = range(len(bm))
        for idx in sect: others.remove(idx)
            
        for i in range(len(bm)):#sect:
            for j in range(i):#others:
                if i<j: continue
                for k in range(len(bm)):
                    if k==i or k==j or i==j: continue
                    Uik = bm[i,k] if k>i else bm[k,i]
                    Ukj = bm[j,k] if k>j else bm[k,j]
                    
                    maxLij = Uik + Ukj - 0.1
                    if bm[i,j] >  maxLij:
                        print "CHANGING Lower limit {0} to {1}".format(bm[i,j], maxLij)
                        bm[i,j] = maxLij
        
        return bm
    
    def bmTest(self, bm):
        """
        Test to show why a bounds matrix is not valid
        """
        for k in range(len(bm)):
            for i in range(len(bm)-1):
                if i==k: continue
                Uik = bm[i,k] if k>i else bm[k,i]
                Lik = bm[i,k] if i>k else bm[k,i]
                for j in range(i+1, len(bm)):
                    if j==k: continue
                    Ujk = bm[j,k] if k>j else bm[k,j]
                    Ljk = bm[j,k] if j>k else bm[j,k]
                    Uij = bm[i,j] if j>i else bm[j,i]
                    Lij = bm[i,j] if i>j else bm[j,i]
                    sumUikUjk = Uik + Ujk
                    if Uij > sumUikUjk:
                        print "Upper limit for {i} and {j} is too high".format(i=i, j=j)
                    
                    diffLikUjk = Lik - Ujk
                    diffLjkUik = Ljk - Uik
                    if Uij < diffLikUjk or Uik < diffLjkUik:
                        print "Lower limit for {i} and {j} is too low".format(i=i, j=j)
    
    def editDoubMatrix(self, reactant, product, bm1, bm2):
        """
        For bimolecular reactions, reduce the minimum distance between atoms
        of the two reactanting species, in preparation for a double-ended search.
        `bm1` is typically the bounds matrix for the reactant side, and `bm2` for
        the products. 
        """
        def fixMatrix(bm, lbl1, lbl2, lbl3, num, diff):
            if lbl2 > lbl1:
                dnDiff = bm[lbl1][lbl2]
                upDiff = bm[lbl2][lbl1]
            else:
                dnDiff = bm[lbl2][lbl1]
                upDiff = bm[lbl1][lbl2]
            
            if lbl1 > lbl3:
                bm[lbl3][lbl1] = num + diff/2.
                bm[lbl1][lbl3] = num - diff/2.
                if lbl2 > lbl3:
                    bm[lbl3][lbl2] = bm[lbl3][lbl1] - upDiff
                    bm[lbl2][lbl3] = bm[lbl1][lbl3] - dnDiff
                else:
                    bm[lbl2][lbl3] = bm[lbl3][lbl1] - upDiff
                    bm[lbl3][lbl2] = bm[lbl1][lbl3] - dnDiff
            else:
                bm[lbl1][lbl3] = num + diff/2.
                bm[lbl3][lbl1] = num - diff/2.
                if lbl2 > lbl3:
                    bm[lbl3][lbl2] = bm[lbl1][lbl3] - upDiff
                    bm[lbl2][lbl3] = bm[lbl3][lbl1] - dnDiff
                else:
                    bm[lbl2][lbl3] = bm[lbl1][lbl3] - upDiff
                    bm[lbl3][lbl2] = bm[lbl3][lbl1] - dnDiff
            return bm
        
        if self.reaction.label.lower() == 'h_abstraction':
            
            lbl1 = reactant.getLabeledAtom('*1').sortingLabel
            lbl2 = reactant.getLabeledAtom('*2').sortingLabel
            lbl3 = reactant.getLabeledAtom('*3').sortingLabel
        
        elif self.reaction.label.lower() == 'disproportionation':
            
            lbl1 = reactant.getLabeledAtom('*2').sortingLabel
            lbl2 = reactant.getLabeledAtom('*4').sortingLabel
            lbl3 = reactant.getLabeledAtom('*1').sortingLabel
            
        labels = [lbl1, lbl2, lbl3]
        atomMatch = ((lbl1,),(lbl2,),(lbl3,))
        
        #bm1 = fixMatrix(bm1, lbl1, lbl2, lbl3, 3.0, 0.1)
        #bm2 = fixMatrix(bm2, lbl3, lbl2, lbl1, 3.0, 0.1)    
        if reactant.atoms[lbl1].symbol == 'H' or reactant.atoms[lbl3].symbol == 'H':
            bm1 = fixMatrix(bm1, lbl1, lbl2, lbl3, 2.3, 0.1)
            bm2 = fixMatrix(bm2, lbl3, lbl2, lbl1, 2.3, 0.1)
        else:
            bm1 = fixMatrix(bm1, lbl1, lbl2, lbl3, 2.7, 0.1)
            bm2 = fixMatrix(bm2, lbl3, lbl2, lbl1, 2.7, 0.1)
       
        # sect = len(reactant.split()[1].atoms)
        rSect = []
        for atom in reactant.split()[0].atoms: rSect.append(atom.sortingLabel)
        
        pSect = []
        for atom in product.split()[0].atoms: pSect.append(atom.sortingLabel)
            
        bm1 = self.bmPreEdit(bm1, rSect)
        bm2 = self.bmPreEdit(bm2, pSect)
        
        return bm1, bm2, labels, atomMatch
    
    def editMatrix(self, reactant, bm, database):
        
        """
        For bimolecular reactions, reduce the minimum distance between atoms
        of the two reactants. 
        """
        if self.reaction.family.label.lower() in ['h_abstraction', 'r_addition_multiplebond', 'intra_h_migration']:
            
            lbl1 = reactant.getLabeledAtom('*1').sortingLabel
            lbl2 = reactant.getLabeledAtom('*2').sortingLabel
            lbl3 = reactant.getLabeledAtom('*3').sortingLabel
        
        elif self.reaction.family.label.lower() == 'disproportionation':
            
            lbl1 = reactant.getLabeledAtom('*2').sortingLabel
            lbl2 = reactant.getLabeledAtom('*4').sortingLabel
            lbl3 = reactant.getLabeledAtom('*1').sortingLabel
            
        labels = [lbl1, lbl2, lbl3]
        atomMatch = ((lbl1,),(lbl2,),(lbl3,))
        
        
        tsData = database.kinetics.families[self.reaction.family.label]
        distanceData = tsData.transitionStates.estimateDistances(self.reaction)
        
        sect = []
        for atom in reactant.split()[0].atoms: sect.append(atom.sortingLabel)
        
        uncertainties = {'d12':0.1, 'd13':0.1, 'd23':0.1 } # distanceData.uncertainties or {'d12':0.1, 'd13':0.1, 'd23':0.1 } # default if uncertainty is None
        bm = self.setLimits(bm, lbl1, lbl2, distanceData.distances['d12'], uncertainties['d12'])
        bm = self.setLimits(bm, lbl2, lbl3, distanceData.distances['d23'], uncertainties['d23'])
        bm = self.setLimits(bm, lbl1, lbl3, distanceData.distances['d13'], uncertainties['d13'])
        
        bm = self.bmPreEdit(bm, sect)
            
        return bm, labels, atomMatch
        
    def generateTSGeometryDoubleEnded(self, doubleEnd=None):
        """
        Generate a Transition State geometry using the double-ended search method
        
        Returns (mopac, fromDbl, labels, notes) where mopac and fromDbl are 
        booleans (fromDbl is always True), and notes is a string of comments on what happened.
        """
        assert doubleEnd is not None and len(doubleEnd)==2, "You must provide the two ends of the search using 'doubleEnd' argument."
        notes = ''
        if os.path.exists(os.path.join(self.file_store_path, self.uniqueID + '.data')):
            logging.info("Not generating TS geometry because it's already done.")
            return True, None, None, "Already done!"

        reactant = doubleEnd[0]
        product = doubleEnd[1]

        rRDMol, rBM, self.geometry = self.generateBoundsMatrix(reactant)
        pRDMol, pBM, pGeom = self.generateBoundsMatrix(product)
        
        # # Smooth the inital matrix derived in rdkit
        # reactantSmoothingSuccessful = rdkit.DistanceGeometry.DoTriangleSmoothing(rBM)
        # productSmoothingSuccessful = rdkit.DistanceGeometry.DoTriangleSmoothing(pBM)
        
        print "Reactant original matrix (smoothed)"
        print matrixToString(rBM)
        print "Product original matrix (smoothed)"
        print matrixToString(pBM)
        
        self.geometry.uniqueID = self.uniqueID
        rBM, pBM, labels, atomMatch = self.editDoubMatrix(reactant, product, rBM, pBM)
        
        print "Reactant edited matrix"
        print matrixToString(rBM)
        print "Product edited matrix"
        print matrixToString(pBM)
        
        reactantSmoothingSuccessful = rdkit.DistanceGeometry.DoTriangleSmoothing(rBM)
        productSmoothingSuccessful  = rdkit.DistanceGeometry.DoTriangleSmoothing(pBM)
        
        if reactantSmoothingSuccessful:
            print "Reactant matrix is embeddable"
            print "Smoothed reactant matrix"
            print matrixToString(rBM)
        else:
            print "Reactant matrix is NOT embeddable"
        if productSmoothingSuccessful:
            print "Product matrix is embeddable"
            print "Smoothed product matrix"
            print matrixToString(pBM)
        else:
            print "Product matrix is NOT embeddable"
            
        if not (reactantSmoothingSuccessful and productSmoothingSuccessful):
            notes = 'Bounds matrix editing failed\n'
            return False, None, None, notes
        
        atoms = len(reactant.atoms)
        distGeomAttempts = 15*(atoms-3) # number of conformers embedded from the bounds matrix
         
        rdmol, minEid = self.geometry.rd_embed(rRDMol, distGeomAttempts, bm=rBM, match=atomMatch)
        if not rdmol:
            print "RDKit failed all attempts to embed"
            notes = notes + "RDKit failed all attempts to embed"
            return False, None, None, notes
        rRDMol = rdkit.Chem.MolFromMolFile(self.geometry.getCrudeMolFilePath(), removeHs=False)
        # Make product pRDMol a copy of the reactant rRDMol geometry
        for atom in reactant.atoms:
            i = atom.sortingLabel
            pRDMol.GetConformer(0).SetAtomPosition(i, rRDMol.GetConformer(0).GetAtomPosition(i))

        # don't re-embed the product, just optimize at UFF, constrained with the correct bounds matrix
        pRDMol, minEid = pGeom.optimize(pRDMol, boundsMatrix=pBM, atomMatch=atomMatch)
        pGeom.writeMolFile(pRDMol, pGeom.getRefinedMolFilePath(), minEid)
             
        if os.path.exists(self.outputFilePath):
            logging.info("File {0} already exists.".format(self.outputFilePath))
            # I'm not sure why that should be a problem, but we used to do nothin in this case
            notes = notes + 'Already have an output, check the IRC\n'
            rightTS = self.verifyIRCOutputFile()
            if rightTS:
                self.writeRxnOutputFile(labels)
                return True, self.geometry, labels, notes
            else:
                return False, None, None, notes

        if self.settings.software.lower() == 'mopac':
            # all below needs to change
            print "Optimizing reactant geometry"
            self.writeGeomInputFile(freezeAtoms=labels)
            logFilePath = self.runDouble(self.inputFilePath)
            shutil.copy(logFilePath, logFilePath+'.reactant.out')
            print "Optimizing product geometry"
            self.writeGeomInputFile(freezeAtoms=labels, otherGeom=pGeom)
            logFilePath = self.runDouble(pGeom.getFilePath(self.inputFileExtension))
            shutil.copy(logFilePath, logFilePath+'.product.out')
                
            print "Product geometry referencing reactant"
            self.writeReferenceFile()#inputFilePath, molFilePathForCalc, geometry, attempt, outputFile=None)
            self.writeGeoRefInputFile(pGeom, otherSide=True)#inputFilePath, molFilePathForCalc, refFilePath, geometry)
            logFilePath = self.runDouble(pGeom.getFilePath(self.inputFileExtension))
            shutil.copy(logFilePath, logFilePath+'.ref1.out')
                
            if not os.path.exists(pGeom.getFilePath('.arc')):
                notes = notes + 'product .arc file does not exits\n'
                return False, None, None, notes
            
            # Reactant that references the product geometry
            print "Reactant referencing product on slope"
            self.writeReferenceFile(otherGeom=pGeom)
            self.writeGeoRefInputFile(pGeom)
            logFilePath = self.runDouble(self.inputFilePath)
            shutil.copy(logFilePath, logFilePath+'.ref2.out')
            
            if not os.path.exists(self.getFilePath('.arc')):
                notes = notes + 'reactant .arc file does not exits\n'
                return False, None, None, notes
            
            # Write saddle calculation file using the outputs of the reference calculations
            print "Running Saddle from optimized geometries"
            self.writeSaddleInputFile(pGeom)
            self.runDouble(self.inputFilePath)
            return True, self.geometry, labels, notes
        elif self.settings.software.lower() == 'gaussian':
            # all below needs to change
            print "Optimizing reactant geometry"
            self.writeGeomInputFile(freezeAtoms=labels)
            logFilePath = self.runDouble(self.inputFilePath)
            rightReactant = self.checkGeometry(logFilePath, self.geometry.molecule)
            shutil.copy(logFilePath, logFilePath+'.reactant.log')
            
            print "Optimizing product geometry"
            self.writeGeomInputFile(freezeAtoms=labels, otherGeom=pGeom)
            logFilePath = self.runDouble(pGeom.getFilePath(self.inputFileExtension))
            rightProduct = self.checkGeometry(logFilePath, pGeom.molecule)
            shutil.copy(logFilePath, logFilePath+'.product.log')
            
            if not (rightReactant and rightProduct):
                if not rightReactant:
                    print "Reactant geometry failure, see:" + self.settings.fileStore
                    notes = notes + 'Reactant geometry failure\n'
                else:
                    print "Reactant geometry success"
                
                if not rightProduct:
                    print "Product geometry failure, see:" + self.settings.fileStore
                    notes = notes + 'Product geometry failure\n'
                else:
                    print "Product geometry success"
                # Don't run if the geometries have optimized to another geometry
                return False, None, None, notes
                
            print "Running QST2 from optimized geometries"
            self.writeQST2InputFile(pGeom)
            qst2, logFilePath = self.runQST2()
            shutil.copy(logFilePath, logFilePath+'.QST2.log')
            
            if not qst2:
                print "QST3 needed, see:" + self.settings.fileStore
                notes = notes + 'QST3 needed\n'
                return False, None, None, notes
                
            print "Optimizing TS once"
            self.writeInputFile(1, fromQST2=True)
            converged, internalCoord = self.run()
            shutil.copy(self.outputFilePath, self.outputFilePath+'.TS1.log')
            
            if internalCoord and not converged:
                print "Internal coordinate error, trying in cartesian"
                self.writeInputFile(2, fromQST2=True)
                converged, internalCoord = self.run()
            
            if not converged:
                notes = notes + 'Transition state failed\n'
                return False, None, None, notes
            
            if os.path.exists(self.ircOutputFilePath):
                rightTS = self.verifyIRCOutputFile()
            else:
                self.writeIRCFile()
                rightTS = self.runIRC()
            
            if not rightTS:
                notes = notes + 'IRC failed\n'
                return False, None, None, notes
            
            self.writeRxnOutputFile(labels, doubleEnd=True)
            return True, None, None, notes
        else:
            raise NotImplementedError("self.settings.software.lower() should be gaussian or mopac")
            return False, None, None, notes
    
    # def runNEB(self, pGeom):
    #     """
    #     Takes the reactant geometry (in `self`) and the product geometry (`pGeom`)
    #     and does an interpolation. This can run nudged-elastic band calculations using
    #     the Atomic Simulation Environment (`ASE <https://wiki.fysik.dtu.dk/ase/>`).
    #     """
                
    def generateTSGeometryNEB(self, doubleEnd=None):
        """
        Generate a Transition State geometry using the double-ended search method
        
        Returns (mopac, fromDbl, labels, notes) where mopac and fromDbl are 
        booleans (fromDbl is always True), and notes is a string of comments on what happened.
        """
        import mpi4py
        
        import sys
        
        import ase
        from ase.neb import NEB
        from ase.parallel import rank, size, world
        from ase.optimize import BFGS, FIRE
        from ase.io.trajectory import PickleTrajectory
        
        if rank==0:
            if not os.path.exists(self.settings.fileStore):
                os.makedirs(self.settings.fileStore)
                
            assert doubleEnd is not None and len(doubleEnd)==2, "You must provide the two ends of the search using 'doubleEnd' argument."
            notes = ''
            if os.path.exists(self.getFilePath('.data')):
                logging.info("Not generating TS geometry because it's already done.")
                return True, None, None, "Already done!"
             
            reactant = doubleEnd[0]
            product = doubleEnd[1]
            
            rRDMol, rBM, self.geometry = self.generateBoundsMatrix(reactant)
            pRDMol, pBM, pGeom = self.generateBoundsMatrix(product)
            
            print "Reactant original matrix (smoothed)"
            print matrixToString(rBM)
            print "Product original matrix (smoothed)"
            print matrixToString(pBM)
            
            self.geometry.uniqueID = self.uniqueID
            rBM, pBM, labels, atomMatch = self.editDoubMatrix(reactant, product, rBM, pBM)
                
            if not os.path.exists(self.getFilePath('peak.xyz')):            
                print "Reactant edited matrix"
                print matrixToString(rBM)
                print "Product edited matrix"
                print matrixToString(pBM)
                
                reactantSmoothingSuccessful = rdkit.DistanceGeometry.DoTriangleSmoothing(rBM)
                productSmoothingSuccessful  = rdkit.DistanceGeometry.DoTriangleSmoothing(pBM)
                
                if reactantSmoothingSuccessful:
                    print "Reactant matrix is embeddable"
                    print "Smoothed reactant matrix"
                    print matrixToString(rBM)
                else:
                    print "Reactant matrix is NOT embeddable"
                if productSmoothingSuccessful:
                    print "Product matrix is embeddable"
                    print "Smoothed product matrix"
                    print matrixToString(pBM)
                else:
                    print "Product matrix is NOT embeddable"
                    
                if not (reactantSmoothingSuccessful and productSmoothingSuccessful):
                    notes = 'Bounds matrix editing failed\n'
                    return False, None, None, notes
                
                atoms = len(reactant.atoms)
                distGeomAttempts = 15*(atoms-3) # number of conformers embedded from the bounds matrix
                 
                rdmol, minEid = self.geometry.rd_embed(rRDMol, distGeomAttempts, bm=rBM, match=atomMatch)
                if not rdmol:
                    print "RDKit failed all attempts to embed"
                    notes = notes + "RDKit failed all attempts to embed"
                    return False, None, None, notes
                rRDMol = rdkit.Chem.MolFromMolFile(self.geometry.getCrudeMolFilePath(), removeHs=False)
                # Make product pRDMol a copy of the reactant rRDMol geometry
                for atom in reactant.atoms:
                    i = atom.sortingLabel
                    pRDMol.GetConformer(0).SetAtomPosition(i, rRDMol.GetConformer(0).GetAtomPosition(i))
            
                # don't re-embed the product, just optimize at UFF, constrained with the correct bounds matrix
                pRDMol, minEid = pGeom.optimize(pRDMol, boundsMatrix=pBM, atomMatch=atomMatch)
                pGeom.writeMolFile(pRDMol, pGeom.getRefinedMolFilePath(), minEid)
                     
                # if os.path.exists(self.outputFilePath):
                #     logging.info("File {0} already exists.".format(self.outputFilePath))
                #     # I'm not sure why that should be a problem, but we used to do nothin in this case
                #     notes = notes + 'Already have an output, check the IRC\n'
                #     rightTS = self.verifyIRCOutputFile()
                #     if rightTS:
                #         self.writeRxnOutputFile(labels)
                #         return True, self.geometry, labels, notes
                #     else:
                #         return False, None, None, notes
            
                if self.settings.software.lower() == 'gaussian':
                    # all below needs to change
                    if os.path.exists(self.getFilePath('.log.reactant.log')):
                        print "Already have reactant"
                        rightReactant = self.checkGeometry(self.getFilePath('.log.reactant.log'), self.geometry.molecule)
                    else:
                        print "Optimizing reactant geometry"
                        self.writeGeomInputFile(freezeAtoms=labels)
                        logFilePath = self.runDouble(self.inputFilePath)
                        rightReactant = self.checkGeometry(logFilePath, self.geometry.molecule)
                        shutil.copy(logFilePath, logFilePath+'.reactant.log')
                    
                    if os.path.exists(pGeom.getFilePath('.log.product.log')):
                        print "Already have product"
                        rightProduct = self.checkGeometry(pGeom.getFilePath('.log.product.log'), pGeom.molecule)
                    else:
                        print "Optimizing product geometry"
                        self.writeGeomInputFile(freezeAtoms=labels, otherGeom=pGeom)
                        logFilePath = self.runDouble(pGeom.getFilePath(self.inputFileExtension))
                        rightProduct = self.checkGeometry(logFilePath, pGeom.molecule)
                        shutil.copy(logFilePath, logFilePath+'.product.log')
                    
                    if not (rightReactant and rightProduct):
                        if not rightReactant:
                            print "Reactant geometry failure, see:" + self.settings.fileStore
                            notes = notes + 'Reactant geometry failure\n'
                        else:
                            print "Reactant geometry success"
                        
                        if not rightProduct:
                            print "Product geometry failure, see:" + self.settings.fileStore
                            notes = notes + 'Product geometry failure\n'
                        else:
                            print "Product geometry success"
                        # Don't run if the geometries have optimized to another geometry
                        return False, None, None, notes
                elif self.settings.software.lower() == 'mopac':
                    print "Optimizing reactant geometry"
                    self.writeGeomInputFile(freezeAtoms=labels)
                    logFilePath = self.runDouble(self.inputFilePath)
                    shutil.copy(logFilePath, logFilePath+'.reactant.out')
                    
                    print "Optimizing product geometry"
                    self.writeGeomInputFile(freezeAtoms=labels, otherGeom=pGeom)
                    logFilePath = self.runDouble(pGeom.getFilePath(self.inputFileExtension))
                    shutil.copy(logFilePath, logFilePath+'.product.out')
                    
                    
                    # print "Product geometry referencing reactant"
                    # self.writeReferenceFile(freezeAtoms=labels)#inputFilePath, molFilePathForCalc, geometry, attempt, outputFile=None)
                    # self.writeGeoRefInputFile(pGeom, freezeAtoms=labels, otherSide=True)#inputFilePath, molFilePathForCalc, refFilePath, geometry)
                    # logFilePath = self.runDouble(pGeom.getFilePath(self.inputFileExtension))
                    # shutil.copy(logFilePath, logFilePath+'.ref1.out')
                    #     
                    # if not os.path.exists(pGeom.getFilePath('.arc')):
                    #     notes = notes + 'product .arc file does not exits\n'
                    #     return False, None, None, notes
                    # 
                    # # Reactant that references the product geometry
                    # print "Reactant referencing product on slope"
                    # self.writeReferenceFile(freezeAtoms=labels, otherGeom=pGeom)
                    # self.writeGeoRefInputFile(pGeom, freezeAtoms=labels)
                    # logFilePath = self.runDouble(self.inputFilePath)
                    # shutil.copy(logFilePath, logFilePath+'.ref2.out')
                    # 
                    # if not os.path.exists(self.getFilePath('.arc')):
                    #     notes = notes + 'reactant .arc file does not exits\n'
                    #     return False, None, None, notes
                data={'self': self, 'pGeom': pGeom}
                for proc in range(size):
                    if proc!=0:
                        world.comm.send(data, dest=proc, tag=72)
                sys.stdout.write(
                    "Hello world! I am process %d of %d according to %r. I just called all the workers.\n"
                    % (rank, size, world))
        else:
            data = world.comm.recv(source=0, tag=72)
            self = data['self']
            pGeom = data['pGeom']
            sys.stdout.write(
                "Hello world! I am process %d of %d according to %r. I will now start running.\n"
                % (rank, size, world))
                    
        print "Running NEB from optimized geometries"
        # Atomic Simulation Environment can take the two geometries and
        # do the calculation on its own
        
        initial, final = self.setImages(pGeom)
        
        # Set the number of images
        number_of_images = 11
        images = [initial]
        j = rank * number_of_images // size
        n = size // number_of_images
        for i in range(number_of_images):
            image = initial.copy()
            if i==j:
                self.setCalculator(image, rank=rank, parallel=True)
                trackImage = image
            images.append(image)
        
        images.append(final)
        neb = ase.neb.NEB(images, climb=True, parallel=True) #(images,k,climb,parallel,world)
        
        # Interpolate the positions of the middle images linearly, then set calculators
        neb.interpolate()
        
        # self.setCalculator(images)
        if neb.climb:
            optimizer = FIRE(neb)
        else:
            optimizer = BFGS(neb) # (logfile=nebLog)
        
        if rank % number_of_images == 0:
            trajFile = os.path.join(self.settings.fileStore, 'neb%d.traj' % j)
            traj = PickleTrajectory(trajFile, 'w', images[j], master=(rank % n == 0))
            optimizer.attach(traj)
        optimized = True
        try:
            optimizer.run(steps=30)
        except Exception, e:
            print str(e)
            optimized = False
            pass
        
        print "{0} the NEB was optimized".format(optimized)
        if optimized:
            lastNum = number_of_images-1
            if rank==0:
                energies = dict(('{0}'.format(x),None) for x in range(number_of_images))
                energies['{0}'.format(rank)] = neb.images[rank+1].get_potential_energy()
                print "Got energy for rank {0}".format(rank)
                world.comm.send(energies, dest=1, tag=27)
            else:
                energies = world.comm.recv(source=rank-1, tag=27)
                energies['{0}'.format(rank)] = neb.images[rank+1].get_potential_energy()
                print "Got energy for rank {0}".format(rank)
                if rank!=lastNum:
                    world.comm.send(energies, dest=rank+1, tag=27)
            
            # if rank==0:
            #     imageDict = dict(('{0}'.format(x),None) for x in range(number_of_images))
            #     imageDict['{0}'.format(rank)] = neb.images[rank+1]
            #     world.comm.send(imageDict, dest=1, tag=32)
            # else:
            #     imageDict = world.comm.recv(source=rank-1, tag=32)
            #     imageDict['{0}'.format(rank)] = neb.images[rank+1]
            #     if rank!=lastNum:
            #         world.comm.send(imageDict, dest=rank+1, tag=32)
                
            if rank==lastNum:
                assert None not in energies.values()
                # assert None not in imageDict.values()
                energyList = energies.values()
                energyList.sort()
                maxE = energyList[-1]
                
                # Worst case, if it doesn't find the number it's most likely this
                wantedImg = number_of_images/2 
                for key, item in energies.iteritems():
                    if item==maxE:
                        wantedImg=key
                wantedRank = int(wantedImg)+1
                print "Wanted rank is {0}".format(wantedRank)                
                for proc in range(number_of_images):
                    if proc!=lastNum:
                        world.comm.send(wantedRank, dest=proc, tag=5) # last rank sends a signal to all the others
                if wantedRank==lastNum: #in the case where rank is = lastNum
                    energies['{0}'.format(rank)] = neb.images[rank+1].get_potential_energy()
                    positions = neb.images[rank+1].get_positions()
                    symbols = neb.images[rank+1].get_chemical_symbols()
                    print "Got geometries for wanted rank {0}".format(wantedRank)
                    with open(self.getFilePath('peak.xyz'), 'w') as cartesianFile:
                        for i, position in enumerate(positions):
                            cartesianFile.write("{0}  {1: .6f}  {2: .6f}  {3: .6f}\n".format(symbols[i], position[0], position[1], position[2]))
                    data ='go'
                    for proc in range(number_of_images):
                        if proc!=wantedRank:
                            world.comm.send(data, dest=proc, tag=6) # The rank with the highest peak sends a signal to all the others that it's done
                else: #if wantedRank is not lastNum
                    data = world.comm.recv(source=wantedRank, tag=6) # last rank waits here for the final go ahead
            else:
                wantedRank = world.comm.recv(source=lastNum, tag=5) # All but the last rank gets a signal from the last rank
                if wantedRank==rank:
                    energies['{0}'.format(rank)] = neb.images[rank+1].get_potential_energy()
                    positions = neb.images[rank+1].get_positions()
                    symbols = neb.images[rank+1].get_chemical_symbols()
                    print "Got geometries for wanted rank {0}".format(wantedRank)
                    with open(self.getFilePath('peak.xyz'), 'w') as cartesianFile:
                        for i, position in enumerate(positions):
                            cartesianFile.write("{0}  {1: .6f}  {2: .6f}  {3: .6f}\n".format(symbols[i], position[0], position[1], position[2]))
                    data ='go'
                    for proc in range(number_of_images):
                        if proc!=wantedRank:
                            world.comm.send(data, dest=proc, tag=6) # The rank with the highest peak sends a signal to all the others that it's done
                else:
                    data = world.comm.recv(source=wantedRank, tag=6) # The rest go
        else:
            if rank==0:
                print "Not optimized"
        #if optimized:
        #    lastNum = number_of_images-1
        #    if rank==0:
        #        energies = dict(('{0}'.format(x),None) for x in range(number_of_images))
        #        energies['{0}'.format(rank)] = trackImage.get_potential_energy()
        #        world.comm.send(energies, dest=1, tag=27)
        #    else:
        #        energies = world.comm.recv(source=rank-1, tag=27)
        #        energies['{0}'.format(rank)] = trackImage.get_potential_energy()
        #        if rank!=lastNum:
        #            world.comm.send(energies, dest=rank+1, tag=27)
        #    
        #    # if rank==0:
        #    #     imageDict = dict(('{0}'.format(x),None) for x in range(number_of_images))
        #    #     imageDict['{0}'.format(rank)] = neb.images[rank+1]
        #    #     world.comm.send(imageDict, dest=1, tag=32)
        #    # else:
        #    #     imageDict = world.comm.recv(source=rank-1, tag=32)
        #    #     imageDict['{0}'.format(rank)] = neb.images[rank+1]
        #    #     if rank!=lastNum:
        #    #         world.comm.send(imageDict, dest=rank+1, tag=32)
        #        
        #    if rank==lastNum:
        #        assert None not in energies.values()
        #        # assert None not in imageDict.values()
        #        energyList = energies.values()
        #        energyList.sort()
        #        maxE = energyList[-1]
        #        
        #        # Worst case, if it doesn't find the number it's most likely this
        #        wantedImg = number_of_images/2 
        #        for key, item in energies.iteritems():
        #            if item==maxE:
        #                wantedImg=key
        #        wantedRank = int(wantedImg)+1
        #        world.comm.send(wantedRank, dest=wantedRank, tag=5) # last rank sends a signal to all the others
        #        data = world.comm.recv(source=wantedRank, tag=6) # last rank waits here for the final go ahead
        #    else:
        #        wantedRank = world.comm.recv(source=lastNum, tag=5) # All but the last rank gets a signal from the last rank
        #        if wantedRank==rank:
        #            print "Found peak image, writing to file {0}".format(self.getFilePath('peak.xyz'))
        #            trackImage.write(self.getFilePath('peak.xyz'), format='xyz')
        #            assert os.path.exists(self.getFilePath('peak.xyz'))
        #            data ='go'
        #            for proc in range(number_of_images):
        #                if proc!=wantedRank:
        #                    world.comm.send(data, dest=proc, tag=6) # The rank with the highest peak sends a signal to all the others that it's done
        #        else:
        #            data = world.comm.recv(source=wantedRank, tag=6) # The rest go
        
        
        if rank==0:
            print "Completed NEB calculation"
            
            print "Optimizing TS once"
            self.writeInputFile(1, fromNEB=True)
            converged, internalCoord = self.run()
            shutil.copy(self.outputFilePath, self.outputFilePath+'.TS1.log')
            
            if internalCoord and not converged:
                print "Internal coordinate error, trying in cartesian"
                self.writeInputFile(2, fromInt=True)
                converged, internalCoord = self.run()
                shutil.copy(self.outputFilePath, self.outputFilePath+'.TS2.log')
            
            if not converged:
                notes = notes + 'Transition state failed\n'
                return False, notes
            
            if os.path.exists(self.ircOutputFilePath):
                rightTS = self.verifyIRCOutputFile()
            else:
                self.writeIRCFile()
                rightTS = self.runIRC()
            
            if not rightTS:
                notes = notes + 'IRC failed\n'
                return False, notes
            
            self.writeRxnOutputFile(labels, doubleEnd=True)
            return True, notes
        #if optimized:
        #    energies = numpy.empty(neb.nimages - 2)
        #    for i in range(1, neb.nimages - 1):
        #        energies[i - 1] = neb.images[i].get_potential_energy()
        #    imax = 1 + numpy.argsort(energies)[-1]
        #    image = neb.images[imax]
        #    image.write(self.getFilePath('peak.xyz'), format='xyz')
        #else:
        #    print "Not optimized"
        #
        #print "Completed NEB calculation"
        #
        #print "Optimizing TS once"
        #self.writeInputFile(1, fromNEB=True)
        #converged, internalCoord = self.run()
        #shutil.copy(self.outputFilePath, self.outputFilePath+'.TS1.log')
        #
        #if internalCoord and not converged:
        #    print "Internal coordinate error, trying in cartesian"
        #    self.writeInputFile(2, fromInt=True)
        #    converged, internalCoord = self.run()
        #    shutil.copy(self.outputFilePath, self.outputFilePath+'.TS2.log')
        #
        #if not converged:
        #    notes = notes + 'Transition state failed\n'
        #    return False, None, None, notes
        #
        #if os.path.exists(self.ircOutputFilePath):
        #    rightTS = self.verifyIRCOutputFile()
        #else:
        #    self.writeIRCFile()
        #    rightTS = self.runIRC()
        #
        #if not rightTS:
        #    notes = notes + 'IRC failed\n'
        #    return False, None, None, notes
        #
        #self.writeRxnOutputFile(labels, doubleEnd=True)
        #return True, None, None, notes


    def generateTSGeometryDirectGuess(self, database):
        """
        Generate a transition state geometry, using the direct guess (group additive) method.
        
        Returns (success, notes) where success is a True if it worked, else False,
        and notes is a string describing what happened.
        """
        notes = ''
        if os.path.exists(os.path.join(self.file_store_path, self.uniqueID + '.data')):
            logging.info("Not generating TS geometry because it's already done.")
            return True, "Already done!"
        
        if len(self.reaction.reactants)==2:
            if isinstance(self.reaction.reactants[0], Molecule):
                reactant = self.reaction.reactants[0].merge(self.reaction.reactants[1])
            elif isinstance(self.reaction.reactants[0], Species):
                reactant = self.reaction.reactants[0].molecule[0].merge(self.reaction.reactants[1].molecule[0])
        else:
            if isinstance(self.reaction.reactants[0], Molecule):
                reactant = self.reaction.reactants[0]
            elif isinstance(self.reaction.reactants[0], Species):
                reactant = self.reaction.reactants[0].molecule[0]
        
        if len(self.reaction.products)==2:
            if isinstance(self.reaction.reactants[0], Molecule):
                product = self.reaction.products[0].merge(self.reaction.products[1])
            elif isinstance(self.reaction.reactants[0], Species):
                product = self.reaction.products[0].molecule[0].merge(self.reaction.products[1].molecule[0])
        else:
            if isinstance(self.reaction.reactants[0], Molecule):
                product = self.reaction.products[0]
            elif isinstance(self.reaction.reactants[0], Species):
                product = self.reaction.products[0].molecule[0]
            
        reactant = self.fixSortLabel(reactant)
        product = self.fixSortLabel(product)
        
        tsRDMol, tsBM, self.geometry = self.generateBoundsMatrix(reactant)
        
        self.geometry.uniqueID = self.uniqueID
        
        tsBM, labels, atomMatch = self.editMatrix(reactant, tsBM, database)
        atoms = len(reactant.atoms)
        distGeomAttempts = 15*(atoms-3) # number of conformers embedded from the bounds matrix
        
        setBM = rdkit.DistanceGeometry.DoTriangleSmoothing(tsBM)
        
        if setBM:
            for i in range(len(tsBM)):
                for j in range(i,len(tsBM)):
                    if tsBM[j,i] > tsBM[i,j]:
                            print "BOUNDS MATRIX FLAWED {0}>{1}".format(tsBM[j,i], tsBM[i,j])
        
            self.geometry.rd_embed(tsRDMol, distGeomAttempts, bm=tsBM, match=atomMatch)
            
            if not os.path.exists(self.outputFilePath):
                print "Optimizing TS once"
                self.writeInputFile(1)
                converged, internalCoord = self.run()
            else:
                converged, internalCoord = self.verifyOutputFile()
            
            if internalCoord and not converged:
                notes = 'Internal coordinate error, trying cartesian\n'
                print "Optimizing TS in cartesian"
                shutil.copy(self.outputFilePath, self.outputFilePath+'.TS1.log')
                self.writeInputFile(2)
                converged = self.run()
                
            if converged:
                notes = 'TS converged, now for IRC\n'
                print "IRC calculation"
                if not os.path.exists(self.ircOutputFilePath):
                    self.writeIRCFile()
                    rightTS = self.runIRC()
                else:
                    rightTS = self.verifyIRCOutputFile()
                if rightTS:
                    print "Found a transition state"
                    notes = 'Success\n'
                    self.writeRxnOutputFile(labels)
                    return True, notes
                else:
                    print "Graph matching failed"
                    notes = 'IRC failed\n'
                    return False, notes
            else:
                print "TS failed"
                notes = 'TS not converged\n'
                return False, notes
        else:
            notes = 'Bounds matrix editing failed\n'
            return False, notes
    
    def generateTSGeometryTest(self):
        """
        Generate a transition state geometry, using the direct guess (group additive) method.
        
        Returns (success, notes) where success is a True if it worked, else False,
        and notes is a string describing what happened.
        """
        notes = ''
        if os.path.exists(os.path.join(self.file_store_path, self.uniqueID + '.data')):
            logging.info("Not generating TS geometry because it's already done.")
            return True, "Already done!"
        
        if len(self.reaction.reactants)==2:
            if isinstance(self.reaction.reactants[0], Molecule):
                reactant = self.reaction.reactants[0].merge(self.reaction.reactants[1])
            elif isinstance(self.reaction.reactants[0], Species):
                reactant = self.reaction.reactants[0].molecule[0].merge(self.reaction.reactants[1].molecule[0])
        else:
            if isinstance(self.reaction.reactants[0], Molecule):
                reactant = self.reaction.reactants[0]
            elif isinstance(self.reaction.reactants[0], Species):
                reactant = self.reaction.reactants[0].molecule[0]
        
        if len(self.reaction.products)==2:
            if isinstance(self.reaction.reactants[0], Molecule):
                product = self.reaction.products[0].merge(self.reaction.products[1])
            elif isinstance(self.reaction.reactants[0], Species):
                product = self.reaction.products[0].molecule[0].merge(self.reaction.products[1].molecule[0])
        else:
            if isinstance(self.reaction.reactants[0], Molecule):
                product = self.reaction.products[0]
            elif isinstance(self.reaction.reactants[0], Species):
                product = self.reaction.products[0].molecule[0]
            
        reactant = self.fixSortLabel(reactant)
        product = self.fixSortLabel(product)
        
        tsRDMol, tsBM, self.geometry = self.generateBoundsMatrix(reactant)
        
        self.geometry.uniqueID = self.uniqueID
        
        tsBM, labels, atomMatch = self.editMatrix(reactant, tsBM)
        atoms = len(reactant.atoms)
        distGeomAttempts = 15*(atoms-3) # number of conformers embedded from the bounds matrix
        
        setBM = rdkit.DistanceGeometry.DoTriangleSmoothing(tsBM)
        
        if setBM:
            for i in range(len(tsBM)):
                for j in range(i,len(tsBM)):
                    if tsBM[j,i] > tsBM[i,j]:
                            print "BOUNDS MATRIX FLAWED {0}>{1}".format(tsBM[j,i], tsBM[i,j])
        
            self.geometry.rd_embed(tsRDMol, distGeomAttempts, bm=tsBM, match=atomMatch)
            
            if not os.path.exists(self.outputFilePath):
                print "Optimizing TS once"
                self.writeInputFile(1)
                converged, internalCoord = self.run()
            else:
                converged, internalCoord = self.verifyOutputFile()
                longDist = self.testTSGeometry(reactant)
            
            if internalCoord and not converged:
                notes = 'Internal coordinate error, trying cartesian\n'
                print "Optimizing TS in cartesian"
                shutil.copy(self.outputFilePath, self.outputFilePath+'.TS1.log')
                self.writeInputFile(2)
                converged = self.run()
                
            if converged:
                notes = 'TS converged, now for IRC\n'
                print "IRC calculation"
                if not os.path.exists(self.ircOutputFilePath):
                    self.writeIRCFile()
                    rightTS = self.runIRC()
                else:
                    rightTS = self.verifyIRCOutputFile()
                if rightTS:
                    print "Found a transition state"
                    notes = 'Success\n'
                    self.writeRxnOutputFile(labels)
                    return True, notes
                else:
                    print "Graph matching failed"
                    notes = 'IRC failed\n'
                    return False, notes
            else:
                print "TS failed"
                notes = 'TS not converged\n'
                return False, notes
        else:
            notes = 'Bounds matrix editing failed\n'
            return False, notes
    
    def calculateQMData(self, moleculeList):
        """
        If the transition state is found, optimize reactant and product geometries for use in
        TST calculations.
        """
        molecules = []
        
        for molecule in moleculeList:
            molecule = self.fixSortLabel(molecule)
            qmMolecule = self.getQMMolecule(molecule)
            result = qmMolecule.generateQMData()
            if result:
                log = GaussianLog(qmMolecule.outputFilePath)
                species = Species(label=qmMolecule.molecule.toSMILES(), conformer=log.loadConformer(), molecule=[molecule])
                molecules.append(species)
        return molecules
    
    def calculateKinetics(self):
        # provides transitionstate geometry
        tsFound = self.generateTSGeometryDirectGuess()
        
        if not tsFound:
            # fall back on group additivity
            return None
            
        reactants = self.calculateQMData(self.reaction.reactants)
        products = self.calculateQMData(self.reaction.products)
        
        if len(reactants)==len(self.reaction.reactants) and len(products)==len(self.reaction.products):
            #self.determinePointGroup()
            tsLog = GaussianLog(self.outputFilePath)
            self.reaction.transitionState = TransitionState(label=self.uniqueID + 'TS', conformer=tsLog.loadConformer(), frequency=(tsLog.loadNegativeFrequency(), 'cm^-1'), tunneling=Wigner(frequency=None))
                            
            self.reaction.reactants = reactants
            self.reaction.products = products

            
            kineticsJob = KineticsJob(self.reaction)
            kineticsJob.generateKinetics()
            
            """
            What do I do with it? For now just save it.
            Various parameters are not considered in the calculations so far e.g. symmetry.
            This is just a crude calculation, calculating the partition functions
            from the molecular properties and plugging them through the equation. 
            """
            kineticsJob.save(self.getFilePath('.kinetics'))
            # return self.reaction.kinetics     
    
    def determinePointGroup(self):
        """
        Determine point group using the SYMMETRY Program
        
        Stores the resulting :class:`PointGroup` in self.pointGroup
        """
        assert self.qmData, "Need QM Data first in order to calculate point group."
        pgc = symmetry.PointGroupCalculator(self.settings, self.uniqueID, self.qmData)
        self.pointGroup = pgc.calculate()
